import os
import asyncio
import logging
import re
import time
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from telegram.error import Forbidden

TOKEN = "8755292870:AAGYFk5t_MddvDfN0lgZnhsDMTQYDxJI0Ps"

# الرابط الخاص بسيرفرك على Render
SERVER_URL = "https://linkedin-bot-9v0k.onrender.com" 

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- قاعدة البيانات اللحظية (في الـ RAM لسرعة الاختبار) ---
active_posts = []           # قائمة كل البوستات المنشورة [{"id": 1, "user": "Ali", "user_id": 123, "link": "https://..."}]
user_interactions = {}      # سجل تفاعلات كل عضو {user_id: set(post_ids_completed)}
user_verifications = {}      # حالات الفحص الحالية

GROUP_CHAT_ID = None        # ايدي الجروب
BOT_USERNAME = None         # يوزر نيم البوت

# --- سيرفر التتبع (بوابة التفتيش) ---
async def handle_redirect(request):
    try:
        user_id = int(request.query.get("u", 0))
        post_id = int(request.query.get("p", 0))
        
        if user_id in user_verifications:
            user_data = user_verifications[user_id]
            current_index = user_data["current_index"]
            target_post = user_data["uninteracted_posts"][current_index]
            
            if target_post["id"] == post_id:
                user_data["has_clicked_redirect"] = True
                user_data["link_opened_time"] = time.time()
                raise web.HTTPFound(target_post["link"])
    except web.HTTPFound as redirect:
        raise redirect
    except Exception:
        pass
    
    return web.Response(text="رابط غير صالح أو انتهت الجلسة", status=400)

# دوال مساعدة لبناء الأزرار
def get_verification_keyboard(user_id):
    data = user_verifications[user_id]
    current_index = data["current_index"]
    uninteracted = data["uninteracted_posts"]
    
    target_post = uninteracted[current_index]
    post_owner = target_post["user"]
    post_id = target_post["id"]

    tracking_link = f"{SERVER_URL}/redirect?u={user_id}&p={post_id}"

    keyboard = [
        [InlineKeyboardButton(f"🔗 افتح بوست {post_owner} (خطوة {current_index + 1} من {len(uninteracted)})", url=tracking_link)],
        [InlineKeyboardButton(f"✅ تأكيد التفاعل", callback_data=f"verify_{post_id}")]
    ]
    return InlineKeyboardMarkup(keyboard)

# إرسال خطوة الفحص للعضو في الخاص
async def send_verification_step(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    data = user_verifications[user_id]
    current_index = data["current_index"]
    uninteracted = data["uninteracted_posts"]
    
    text = (
        f"✋ أهلاً {data['user_name']}!\n"
        f"لتتمكن من نشر بوستك، يلزم التفاعل مع البوستات المتبقية عليك:\n\n"
        f"📌 **الخطوة ({current_index + 1} من {len(uninteracted)}):**\n"
        f"افتح الرابط التالي لعمل اللايك، ثم اضغط تأكيد."
    )
    
    return await context.bot.send_message(
        chat_id=user_id,
        text=text,
        reply_markup=get_verification_keyboard(user_id)
    )

# أمر /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name

    if user_id in user_verifications:
        await send_verification_step(context, user_id)
    else:
        await update.message.reply_text(
            f"أهلاً بك يا {user_name}! 👋\n"
            "أنا بوت تنظيم تفاعل LinkedIn.\n"
            "عند إرسال أي رابط في الجروب، سأتحقق من تفاعلك مع جميع البوستات السابقة ونشر رابطك فوراً! 🚀"
        )

# استقبال الرسائل في الجروب
async def check_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global GROUP_CHAT_ID, BOT_USERNAME
    message = update.message
    if not message or not message.text:
        return

    if message.chat.type in ["group", "supergroup"]:
        GROUP_CHAT_ID = message.chat_id
        if not BOT_USERNAME:
            bot_info = await context.bot.get_me()
            BOT_USERNAME = bot_info.username

        text = message.text
        user_id = message.from_user.id
        user_name = message.from_user.first_name

        linkedin_pattern = r"(https?://[^\s]*(?:linkedin\.com|lnkd\.in)[^\s]*)"
        matches = re.findall(linkedin_pattern, text, re.IGNORECASE)

        if not matches:
            try:
                await message.delete()
            except Exception:
                pass
            return

        valid_link = matches[0]

        # مسح رسالة العضو فوراً ليبقى الجروب نظيفاً
        try:
            await message.delete()
        except Exception:
            pass

        # حساب البوستات التي لم يتفاعل معها العضو بعد
        completed_set = user_interactions.get(user_id, set())
        uninteracted_posts = [
            p for p in active_posts 
            if p["user_id"] != user_id and p["id"] not in completed_set
        ]

        # --- الحالة الأولى: العضو متفاعل مع كل حاجة (معندوش ديون) ---
        if len(uninteracted_posts) == 0:
            new_post_id = len(active_posts) + 1
            new_post = {
                "id": new_post_id,
                "user": user_name,
                "user_id": user_id,
                "link": valid_link
            }
            active_posts.append(new_post)

            # نشر البوست في الجروب فوراً
            await context.bot.send_message(
                chat_id=message.chat_id,
                text=f"🚀 **بوست جديد تم نشره!**\n\n👤 الناشر: {user_name}\n🔗 الرابط: {valid_link}"
            )

            # إشعار العضو في الخاص
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🎉 **عاش يا {user_name}!** لأنك متفاعل مع جميع البوستات السابقة، تم نشر بوستك فوراً في الجروب! 🚀"
                )
            except Forbidden:
                pass
            return

        # --- الحالة الثانية: العضو عنده ديون تفاعل ---
        user_verifications[user_id] = {
            "pending_link": valid_link,
            "user_name": user_name,
            "uninteracted_posts": uninteracted_posts,
            "current_index": 0,
            "has_clicked_redirect": False,
            "link_opened_time": 0
        }

        # المحاولة الأولى: الإرسال للخاص مباشرة
        try:
            await send_verification_step(context, user_id)
        except Forbidden:
            # لو العضو ما داسش Start للبوت قبل كدة، تلجرام بيرفض.. فنبعتله زرار مؤقت في الجروب يدخله الخاص
            btn = InlineKeyboardMarkup([[
                InlineKeyboardButton("📩 اضغط هنا لبدء الفحص بالخاص", url=f"https://t.me/{BOT_USERNAME}?start=verify")
            ]])
            temp_msg = await context.bot.send_message(
                chat_id=message.chat_id,
                text=f"⚠️ يا {user_name}، يرجى الضغط على الزر بالأسفل لبدء فحص التفاعل بالخاص ونشر بوستك!",
                reply_markup=btn
            )
            await asyncio.sleep(20)
            try:
                await temp_msg.delete()
            except Exception:
                pass

# معالجة أزرار التأكيد في الخاص
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    if user_id not in user_verifications:
        await query.answer("⚠️ ليس لديك عملية فحص معلقة حالياً.", show_alert=True)
        return

    user_data = user_verifications[user_id]
    current_index = user_data["current_index"]
    uninteracted = user_data["uninteracted_posts"]
    current_post = uninteracted[current_index]

    if data.startswith("verify_"):
        # 1. التأكد من فتح الرابط
        if not user_data["has_clicked_redirect"]:
            await query.answer(
                "❌ لم يتم فتح الرابط!\n\nيجب الضغط على زرار (🔗 افتح بوست) والتأكد من تحويلك للصفحة أولاً.",
                show_alert=True
            )
            return

        # 2. التأكد من قضاء 4 ثوانٍ على الأقل
        elapsed_time = time.time() - user_data["link_opened_time"]
        if elapsed_time < 4:
            user_data["has_clicked_redirect"] = False
            user_data["link_opened_time"] = 0
            
            await query.answer(
                "❌ فشل فحص التفاعل!\n\n"
                "تم رصد خروج سريع قبل تسجيل اللايك على البوست.\n"
                "اضغط على زر (🔗 افتح بوست) مجدداً واعمل اللايك قبل التأكيد.",
                show_alert=True
            )
            return

        # تسجيل البوست كـ "مكتمل التفاعل" للعضو
        if user_id not in user_interactions:
            user_interactions[user_id] = set()
        user_interactions[user_id].add(current_post["id"])

        user_data["current_index"] += 1
        user_data["has_clicked_redirect"] = False

        # لو خلص كل البوستات المتبقية عليه
        if user_data["current_index"] >= len(uninteracted):
            pending_link = user_data["pending_link"]
            user_name = user_data["user_name"]

            # إضافة بوست العضو للقائمة العامة
            new_post_id = len(active_posts) + 1
            active_posts.append({
                "id": new_post_id,
                "user": user_name,
                "user_id": user_id,
                "link": pending_link
            })

            # نشر البوست فوراً في الجروب
            if GROUP_CHAT_ID:
                await context.bot.send_message(
                    chat_id=GROUP_CHAT_ID,
                    text=f"🚀 **بوست جديد تم نشره!**\n\n👤 الناشر: {user_name}\n🔗 الرابط: {pending_link}"
                )

            # رسالة النجاح في الخاص
            await query.message.edit_text(
                f"🎉 **تم بنجاح يا {user_name}!**\n\n"
                f"✅ اجتزت فحص جميع البوستات السابقة، وتم نشر بوستك الآن فوراً في الجروب! 🚀"
            )

            del user_verifications[user_id]
        else:
            # الانتقال للبوست التالي
            next_index = user_data["current_index"]
            await query.answer(f"✅ تم تأكيد التفاعل للبوست ({next_index} من {len(uninteracted)})!")
            
            target_next = uninteracted[next_index]
            post_owner = target_next["user"]
            post_id = target_next["id"]
            tracking_link = f"{SERVER_URL}/redirect?u={user_id}&p={post_id}"

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"🔗 افتح بوست {post_owner} (خطوة {next_index + 1} من {len(uninteracted)})", url=tracking_link)],
                [InlineKeyboardButton(f"✅ تأكيد التفاعل", callback_data=f"verify_{post_id}")]
            ])

            await query.message.edit_text(
                text=f"✋ استمر يا {user_data['user_name']}!\n\n"
                     f"📌 **الخطوة ({next_index + 1} من {len(uninteracted)}):**\n"
                     f"افتح الرابط التالي لعمل اللايك ثم اضغط تأكيد.",
                reply_markup=keyboard
            )

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

    await asyncio.Event().wait()

if __name__ == '__main__':
    asyncio.run(main())
