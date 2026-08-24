import os
import asyncio
import logging
import re
import time
from datetime import date
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from telegram.error import Forbidden

TOKEN = "8755292870:AAGYFk5t_MddvDfN0lgZnhsDMTQYDxJI0Ps"

# الرابط الخاص بسيرفرك على Render
SERVER_URL = "https://linkedin-bot-9v0k.onrender.com" 

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

recent_links = []             # آخر لينكات نزلت الجروب للفحص
user_verifications = {}       # حالات الفحص الحالية
user_daily_posts = {}         # تتبع النشر اليومي {user_id: "YYYY-MM-DD"}
posts_queue = []              # طابور الانتظار للينكات المقبولة
last_post_time = 0            # توقيت آخر بوست تم نشره في الجروب
GROUP_CHAT_ID = None          # سيتم حفظ ايدي الجروب تلقائياً للنشر فيه
BOT_USERNAME = None           # يورزر نيم البوت للربط العميق

COOLDOWN_SECONDS = 1800       # 30 دقيقة بين كل بوست والثاني

# --- سيرفر التتبع (بوابة التفتيش) ---
async def handle_redirect(request):
    try:
        user_id = int(request.query.get("u", 0))
        
        if user_id in user_verifications:
            user_data = user_verifications[user_id]
            current_step = user_data["current_step"]
            
            user_data["has_clicked_redirect"] = True
            user_data["link_opened_time"] = time.time()
            
            target_link = user_data["links_to_verify"][current_step]["link"]
            raise web.HTTPFound(target_link)
    except web.HTTPFound as redirect:
        raise redirect
    except Exception:
        pass
    
    return web.Response(text="رابط غير صالحة أو انتهت الجلسة", status=400)

def get_keyboard(user_id):
    data = user_verifications[user_id]
    current_step = data["current_step"]
    
    if current_step >= 3:
        return None 
        
    last_3 = data["links_to_verify"]
    current_owner = last_3[current_step]["user"]

    tracking_link = f"{SERVER_URL}/redirect?u={user_id}"

    keyboard = [
        [InlineKeyboardButton(f"🔗 افتح بوست {current_owner} (رقم {current_step+1})", url=tracking_link)],
        [InlineKeyboardButton(f"✅ تأكيد التفاعل لبوست {current_step+1}", callback_data=f"verify_{current_step}")]
    ]
    return InlineKeyboardMarkup(keyboard)

# أمر /start - بيشتغل لما العضو يضغط على زرار الخاص
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name

    # لو العضو عنده فحص معلق (جاي من الجروب)
    if user_id in user_verifications:
        await update.message.reply_text(
            f"✋ أهلاً يا {user_name}، جاري فحص تفاعلك قبل نشر بوستك في الجروب!\n\n"
            f"📌 **الخطوة 1 من 3:**\n"
            f"افتح الرابط التالي واعمل اللايك، ثم اضغط تأكيد.",
            reply_markup=get_keyboard(user_id)
        )
    else:
        await update.message.reply_text(
            "أهلاً بك! 👋\n"
            "أنا بوت تنظيم تفاعل جروب LinkedIn.\n"
            "انشر رابطك في الجروب وسأقوم بتحويلك هنا للفحص التلقائي ونشره للجميع! 🚀"
        )

async def check_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global GROUP_CHAT_ID, BOT_USERNAME
    message = update.message
    if not message or not message.text:
        return

    # حفظ ايدي الجروب اسم البوت
    if message.chat.type in ["group", "supergroup"]:
        GROUP_CHAT_ID = message.chat_id
        if not BOT_USERNAME:
            bot_info = await context.bot.get_me()
            BOT_USERNAME = bot_info.username

    text = message.text
    user_id = message.from_user.id
    user_name = message.from_user.first_name
    today_str = str(date.today())

    linkedin_pattern = r"(https?://[^\s]*(?:linkedin\.com|lnkd\.in)[^\s]*)"
    matches = re.findall(linkedin_pattern, text, re.IGNORECASE)

    if message.chat.type in ["group", "supergroup"]:
        if not matches:
            try:
                await message.delete()
            except Exception:
                pass
            return

        valid_link = matches[0]

        # مسح رسالة العضو فوراً للنظافة
        try:
            await message.delete()
        except Exception:
            pass

        # 1. شرط اللينك الواحد يومياً
        if user_daily_posts.get(user_id) == today_str:
            btn = InlineKeyboardMarkup([[InlineKeyboardButton("📩 افتح الخاص", url=f"https://t.me/{BOT_USERNAME}")]])
            msg = await context.bot.send_message(
                chat_id=message.chat_id,
                text=f"⚠️ يا {user_name}، مسموح لك بنشر **لينك واحد فقط يومياً**. يمكنك النشر غداً مجدداً!",
                reply_markup=btn
            )
            await asyncio.sleep(15)
            try:
                await msg.delete()
            except Exception:
                pass
            return

        # 2. الجروب في البداية (أقل من 3 لينكات تنزل الجروب فوراً)
        if len(recent_links) < 3:
            recent_links.append({"user": user_name, "link": valid_link})
            user_daily_posts[user_id] = today_str
            
            await context.bot.send_message(
                chat_id=message.chat_id,
                text=f"🚀 **بوست جديد تم نشره!**\n\n👤 الناشر: {user_name}\n🔗 الرابط: {valid_link}"
            )
            return

        # 3. حفظ بيانات الفحص مع تجهيز رابط الانتقال للخاص
        last_3_links = recent_links[-3:]

        user_verifications[user_id] = {
            "pending_link": valid_link,
            "user_name": user_name,
            "current_step": 0,
            "has_clicked_redirect": False,
            "link_opened_time": 0,
            "links_to_verify": last_3_links
        }

        # 4. إرسال زرار توجيه للخاص في الجروب (يمسح نفسه بعد 25 ثانية)
        start_private_btn = InlineKeyboardMarkup([[
            InlineKeyboardButton("📩 اضغط هنا لإكمال الفحص بالخاص", url=f"https://t.me/{BOT_USERNAME}?start=verify")
        ]])
        
        prompt_msg = await context.bot.send_message(
            chat_id=message.chat_id,
            text=f"✋ يا {user_name}، اضغط على الزرار بالأسفل لبدء فحص التفاعل في الخاص لنشر بوستك!",
            reply_markup=start_private_btn
        )

        # مسح التنبيه بعد 25 ثانية ليبقى الجروب نظيفاً
        await asyncio.sleep(25)
        try:
            await prompt_msg.delete()
        except Exception:
            pass

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_post_time
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    if user_id not in user_verifications:
        await query.answer("⚠️ ليس لديك بوست معلق للتحقق منه.", show_alert=True)
        return

    user_data = user_verifications[user_id]

    if data.startswith("verify_"):
        if not user_data["has_clicked_redirect"]:
            await query.answer(
                "❌ لم يتم فتح الرابط!\n\nيجب الضغط على زرار افتح البوست والتأكد من تحويلك للصفحة أولاً.",
                show_alert=True
            )
            return

        elapsed_time = time.time() - user_data["link_opened_time"]
        if elapsed_time < 4:
            user_data["has_clicked_redirect"] = False
            user_data["link_opened_time"] = 0
            
            await query.answer(
                "❌ فشل فحص التفاعل!\n\n"
                "تم رصد خروج سريع قبل تسجيل اللايك على البوست.\n"
                "تم إبطال العملية، اضغط على زر (🔗 افتح بوست) مجدداً واعمل اللايك قبل التأكيد.",
                show_alert=True
            )
            return

        user_data["current_step"] += 1
        user_data["has_clicked_redirect"] = False

        if user_data["current_step"] >= 3:
            pending_link = user_data["pending_link"]
            user_name = user_data["user_name"]
            today_str = str(date.today())

            user_daily_posts[user_id] = today_str

            now = time.time()
            time_since_last = now - last_post_time

            if time_since_last >= COOLDOWN_SECONDS and len(posts_queue) == 0:
                last_post_time = now
                recent_links.append({"user": user_name, "link": pending_link})
                if len(recent_links) > 15:
                    recent_links.pop(0)

                # نشر البوست للجروب
                if GROUP_CHAT_ID:
                    await context.bot.send_message(
                        chat_id=GROUP_CHAT_ID,
                        text=f"🚀 **بوست جديد تم نشره!**\n\n👤 الناشر: {user_name}\n🔗 الرابط: {pending_link}"
                    )

                # رسالة النجاح بالخاص
                await query.message.edit_text(
                    f"🎉 **تم بنجاح يا {user_name}!**\n\n"
                    f"✅ اجتزت الفحص بنجاح، وتم نشر بوستك الآن مباشرة في الجروب!"
                )
            else:
                posts_queue.append({"user": user_name, "link": pending_link, "user_id": user_id})
                queue_position = len(posts_queue)
                
                wait_time_seconds = (COOLDOWN_SECONDS - time_since_last) + ((queue_position - 1) * COOLDOWN_SECONDS)
                wait_minutes = int(wait_time_seconds // 60)

                await query.message.edit_text(
                    f"✅ **تم اجتياز التفاعل بنجاح يا {user_name}!**\n\n"
                    f"📋 **حالة الطابور:**\n"
                    f"بوستك الآن رقم **({queue_position})** في طابور النشر.\n"
                    f"⏱️ وسيتم نشره تلقائياً في الجروب بعد حوالي **{wait_minutes} دقيقة** لضمان أعلى مشاهدة وتفاعل."
                )

            del user_verifications[user_id]
        else:
            next_step = user_data["current_step"]
            await query.answer(f"✅ تم رصد التفاعل لبوست رقم {next_step}!")
            await query.message.edit_text(
                text=f"✋ يا {user_data['user_name']}، استمر!\n\n"
                     f"📌 **الخطوة {next_step + 1} من 3:**\n"
                     f"افتح الرابط التالي لعمل اللايك ثم اضغط تأكيد.",
                reply_markup=get_keyboard(user_id)
            )

# --- محرك الطابور الآلي ---
async def queue_dispatcher(app):
    global last_post_time
    while True:
        await asyncio.sleep(30)
        now = time.time()
        
        if posts_queue and (now - last_post_time >= COOLDOWN_SECONDS) and GROUP_CHAT_ID:
            next_post = posts_queue.pop(0)
            last_post_time = now

            recent_links.append({"user": next_post["user"], "link": next_post["link"]})
            if len(recent_links) > 15:
                recent_links.pop(0)

            try:
                await app.bot.send_message(
                    chat_id=GROUP_CHAT_ID,
                    text=f"📢 **دور النشر وصل من الطابور!**\n\n"
                         f"👤 الناشر: {next_post['user']}\n"
                         f"🔗 الرابط: {next_post['link']}\n\n"
                         f"💡 التفاعل متاح الآن، شاركوا الدعم!"
                )
                
                try:
                    await app.bot.send_message(
                        chat_id=next_post["user_id"],
                        text=f"🔔 **تنبيه:** تم نشر بوستك للتو في الجروب بنجاح! 🚀"
                    )
                except Exception:
                    pass
            except Exception as e:
                logging.error(f"خطأ في إرسال بوست الطابور: {e}")

async def main():
    web_app = web.Application()
    web_app.router.add_get('/redirect', handle_redirect)
    runner = web.AppRunner(web_app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), check_message))
    app.add_handler(CallbackQueryHandler(button_handler))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    asyncio.create_task(queue_dispatcher(app))

    await asyncio.Event().wait()

if __name__ == '__main__':
    asyncio.run(main())
