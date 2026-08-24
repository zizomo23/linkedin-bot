import os
import asyncio
import logging
import re
import time
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters

TOKEN = "8755292870:AAGYFk5t_MddvDfN0lgZnhsDMTQYDxJI0Ps"

# الرابط الخاص بسيرفرك على Render
SERVER_URL = "https://linkedin-bot-9v0k.onrender.com" 

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

recent_links = []
user_verifications = {}

# --- سيرفر التتبع (بوابة التفتيش) ---
async def handle_redirect(request):
    try:
        user_id = int(request.query.get("u", 0))
        
        if user_id in user_verifications:
            user_data = user_verifications[user_id]
            current_step = user_data["current_step"]
            
            # تسجيل فتح اللينك ورصد الوقت
            user_data["has_clicked_redirect"] = True
            user_data["link_opened_time"] = time.time()
            
            target_link = user_data["links_to_verify"][current_step]["link"]
            raise web.HTTPFound(target_link)
    except web.HTTPFound as redirect:
        raise redirect
    except Exception:
        pass
    
    return web.Response(text="رابط غير صالحة أو انتهت الجلسة", status=400)

# --- لوجيك البوت والتفاعل ---
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("أهلاً بك! نظام الفحص الآلي المباشر يعمل الآن 🚀")

async def check_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    text = message.text
    user_id = message.from_user.id
    user_name = message.from_user.first_name

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

        if len(recent_links) < 3:
            recent_links.append({"user": user_name, "link": valid_link})
            await message.reply_text(f"✅ تم نشر لينكك يا {user_name} مباشرة (الجروب في البداية).")
            return

        await message.delete()
        last_3_links = recent_links[-3:]

        user_verifications[user_id] = {
            "pending_link": valid_link,
            "user_name": user_name,
            "current_step": 0,
            "has_clicked_redirect": False,
            "link_opened_time": 0,
            "links_to_verify": last_3_links
        }

        await context.bot.send_message(
            chat_id=message.chat_id,
            text=f"✋ يا {user_name}، جاري فحص التفاعل أولاً!\n\n"
                 f"📌 **الخطوة 1 من 3:**\n"
                 f"افتح الرابط وقم بعمل اللايك، ثم اضغط تأكيد ليتم فحص حسابك.",
            reply_markup=get_keyboard(user_id)
        )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    if user_id not in user_verifications:
        await query.answer("⚠️ ليس لديك بوست معلق للتحقق منه.", show_alert=True)
        return

    user_data = user_verifications[user_id]

    if data.startswith("verify_"):
        # 1. التأكد من الضغط على رابط التتبع
        if not user_data["has_clicked_redirect"]:
            await query.answer(
                "❌ لم يتم فتح الرابط!\n\nيجب الضغط على زرار افتح البوست والتأكد من تحويلك للصفحة أولاً.",
                show_alert=True
            )
            return

        # 2. فحص الوقت (أقل من 4 ثوانٍ = تصفير كامل وإجبار على الضغط مجدداً)
        elapsed_time = time.time() - user_data["link_opened_time"]
        if elapsed_time < 4:
            # إلغاء صلاحية الضغطة السابقة نهائياً
            user_data["has_clicked_redirect"] = False
            user_data["link_opened_time"] = 0
            
            await query.answer(
                "❌ فشل فحص التفاعل!\n\n"
                "تم رصد خروج سريع قبل تسجيل اللايك على البوست.\n"
                "تم إبطال العملية، اضغط على زر (🔗 افتح بوست) مجدداً واعمل اللايك قبل التأكيد.",
                show_alert=True
            )
            return

        # 3. اجتياز الفحص بنجاح
        user_data["current_step"] += 1
        user_data["has_clicked_redirect"] = False # تصفير للبوست التالي

        if user_data["current_step"] >= 3:
            pending_link = user_data["pending_link"]
            user_name = user_data["user_name"]

            recent_links.append({"user": user_name, "link": pending_link})
            if len(recent_links) > 15:
                recent_links.pop(0)

            await query.message.edit_text(
                f"🚀 **بوست جديد تم نشره!**\n\n"
                f"👤 الناشر: {user_name}\n"
                f"🔗 الرابط: {pending_link}\n\n"
                f"✅ تم اجتياز الفحص الآلي للتفاعل بنجاح."
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
