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

# الثوابت الزمنية
DAY_IN_SECONDS = 24 * 3600      # 24 ساعة بالثواني (حد النشر اليومي)
WEEK_IN_SECONDS = 7 * 24 * 3600 # 7 أيام بالثواني (مدة صلاحية الديون)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- قاعدة البيانات اللحظية ---
active_posts = []           # قائمة البوستات [{"id": 1, "user": "Ali", "user_id": 123, "link": "https://...", "timestamp": 1700000000}]
user_interactions = {}      # سجل تفاعلات كل عضو {user_id: set(post_ids_completed)}
user_verifications = {}      # حالات الفحص الحالية بالخاص

GROUP_CHAT_ID = None        # ايدي الجروب
BOT_USERNAME = None         # يوزر نيم البوت

# --- 1️⃣ دالة نشر البوست في الجروب ---
async def publish_post_to_group(context: ContextTypes.DEFAULT_TYPE, post_id: int, user_name: str, link: str):
    if not GROUP_CHAT_ID:
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔗 1. افتح البوست", url=link),
            InlineKeyboardButton("✅ 2. سجّل تفاعلي", callback_data=f"gverify_{post_id}")
        ]
    ])

    await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text=f"🚀 **بوست جديد تم نشره!**\n\n"
             f"👤 الناشر: **{user_name}**\n\n"
             f"🚨 **تنبيه هام جداً (لتسجيل تفاعلك ولعدم الظلم):**\n"
             f"تفاعل مع البوست عن طريق **الأزرار بالأسفل حصراً** ⤵️\n\n"
             f"1️⃣ اضغط على `[ 🔗 1. افتح البوست ]` واعمل اللايك.\n"
             f"2️⃣ ارجع واضغط على `[ ✅ 2. سجّل تفاعلي ]` فوراً.\n\n"
             f"⚠️ *عدم الضغط على زر (سجّل تفاعلي) يعني عدم تسجيل تفاعلك في السيستم وسيطالبك البوت بهذا البوست لاحقاً بالخاص!*",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

# --- 2️⃣ سيرفر التتبع والتحويل المباشر ---
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

# --- 3️⃣ الأوامر العامة ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name

    if user_id in user_verifications:
        await send_verification_step(context, user_id)
    else:
        await update.message.reply_text(
            f"أهلاً بك يا {user_name}! 👋\n"
            "أنا بوت تنظيم تفاعل LinkedIn.\n"
            "يمكنك التفاعل مع البوستات مباشرة في الجروب بالضغط على (سجّل تفاعلي) لخصمها من ديونك تلقائياً! 🚀"
        )

# --- 4️⃣ معالجة الرسائل والروابط في الجروب ---
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
        now = time.time()

        # ========================================================
        # 🔒 فحص الحد اليومي (بوست واحد كل 24 ساعة)
        # ========================================================
        user_posts = [p for p in active_posts if p["user_id"] == user_id]
        if user_posts:
            last_post = max(user_posts, key=lambda x: x["timestamp"])
            time_passed = now - last_post["timestamp"]

            if time_passed < DAY_IN_SECONDS:
                remaining_seconds = DAY_IN_SECONDS - time_passed
                hours = int(remaining_seconds // 3600)
                minutes = int((remaining_seconds % 3600) // 60)

                try:
                    await message.delete()
                except Exception:
                    pass

                time_msg = f"⏳ **المتبقي لتتمكن من النشر مجدداً:** {hours} ساعة و {minutes} دقيقة."

                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"⚠️ **عذراً يا {user_name}!**\n"
                             f"يُسمح بنشر **بوست واحد فقط كل 24 ساعة**.\n\n"
                             f"{time_msg}"
                    )
                except Forbidden:
                    temp_msg = await context.bot.send_message(
                        chat_id=message.chat_id,
                        text=f"⚠️ يا {user_name}، يُسمح بنشر بوست واحد فقط كل 24 ساعة!\n{time_msg}"
                    )
                    await asyncio.sleep(15)
                    try:
                        await temp_msg.delete()
                    except Exception:
                        pass
                return

        # مسح الرسالة الأصلية من الجروب لبدء الإجراءات
        try:
            await message.delete()
        except Exception:
            pass

        completed_set = user_interactions.get(user_id, set())

        # حساب الديون: البوستات في آخر 7 أيام وليست بتاعته ولم يتفاعل معها بعد
        uninteracted_posts = [
            p for p in active_posts 
            if p["user_id"] != user_id 
            and p["id"] not in completed_set
            and (now - p.get("timestamp", 0)) <= WEEK_IN_SECONDS
        ]

        # --- الحالة الأولى: معندوش ديون معلقة ---
        if len(uninteracted_posts) == 0:
            new_post_id = len(active_posts) + 1
            new_post = {
                "id": new_post_id,
                "user": user_name,
                "user_id": user_id,
                "link": valid_link,
                "timestamp": now
            }
            active_posts.append(new_post)

            # نشر البوست فوراً في الجروب
            await publish_post_to_group(context, new_post_id, user_name, valid_link)

            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🎉 **عاش يا {user_name}!** لأنك متفاعل مع جميع البوستات المطلوبة، تم نشر بوستك فوراً في الجروب! 🚀"
                )
            except Forbidden:
                pass
            return

        # --- الحالة الثانية: عنده ديون معلقة ---
        user_verifications[user_id] = {
            "pending_link": valid_link,
            "user_name": user_name,
            "uninteracted_posts": uninteracted_posts,
            "current_index": 0,
            "has_clicked_redirect": False,
            "link_opened_time": 0
        }

        try:
            await send_verification_step(context, user_id)
        except Forbidden:
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

# --- 5️⃣ معالجة الأزرار ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    # 1. زر التسجيل المباشر من الجروب
    if data.startswith("gverify_"):
        post_id = int(data.split("_")[1])
        target_post = next((p for p in active_posts if p["id"] == post_id), None)

        if not target_post:
            await query.answer("⚠️ البوست غير موجود أو قديم جداً.", show_alert=True)
            return

        if target_post["user_id"] == user_id:
            await query.answer("😊 ده بوستك أنت يا بطل! مش محتاج تسجل تفاعلك معاه.", show_alert=True)
            return

        if user_id not in user_interactions:
            user_interactions[user_id] = set()

        if post_id in user_interactions[user_id]:
            await query.answer("⚠️ أنت بالفعل مسجّل تفاعلك مع البوست ده قبل كدا!", show_alert=True)
            return

        user_interactions[user_id].add(post_id)
        await query.answer(
            f"✅ تم تسجيل تفاعلك مع بوست {target_post['user']} بنجاح! 👏\n\n"
            f"اتخصم البوست ده من ديونك، ولما تنزل بوستك هينزل أسرع بدون مشاكل!",
            show_alert=True
        )
        return

    # 2. خطوات الفحص بالخاص
    if user_id not in user_verifications:
        await query.answer("⚠️ ليس لديك عملية فحص معلقة حالياً.", show_alert=True)
        return

    user_data = user_verifications[user_id]
    current_index = user_data["current_index"]
    uninteracted = user_data["uninteracted_posts"]
    current_post = uninteracted[current_index]

    if data.startswith("verify_"):
        if not user_data["has_clicked_redirect"]:
            await query.answer(
                "❌ لم يتم فتح الرابط!\n\nيجب الضغط على زرار (🔗 افتح بوست) والتأكد من تحويلك للصفحة أولاً.",
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
                "اضغط على زر (🔗 افتح بوست) مجدداً واعمل اللايك قبل التأكيد.",
                show_alert=True
            )
            return

        if user_id not in user_interactions:
            user_interactions[user_id] = set()
        user_interactions[user_id].add(current_post["id"])

        user_data["current_index"] += 1
        user_data["has_clicked_redirect"] = False

        if user_data["current_index"] >= len(uninteracted):
            pending_link = user_data["pending_link"]
            user_name = user_data["user_name"]
            now = time.time()

            new_post_id = len(active_posts) + 1
            active_posts.append({
                "id": new_post_id,
                "user": user_name,
                "user_id": user_id,
                "link": pending_link,
                "timestamp": now
            })

            await publish_post_to_group(context, new_post_id, user_name, pending_link)

            await query.message.edit_text(
                f"🎉 **تم بنجاح يا {user_name}!**\n\n"
                f"✅ اجتزت فحص جميع البوستات المطلوبة، وتم نشر بوستك الآن فوراً في الجروب! 🚀"
            )

            del user_verifications[user_id]
        else:
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

# --- 6️⃣ دالة التشغيل الرئيسية ---
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
