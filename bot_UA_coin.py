"""
Telegram бот для дизайнерской компании
Учёт сотрудников: геолокация, отчёты, уведомления
"""

import logging
import asyncio
from datetime import datetime, time
import pytz
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from database import Database
from config import BOT_TOKEN, OFFICE_LAT, OFFICE_LON, OFFICE_RADIUS_M, TIMEZONE

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

db = Database()
tz = pytz.timezone(TIMEZONE)

# ─── КОМАНДЫ ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.user_data["registering"] = True
    await update.message.reply_text(
        "🏢 *SPILNO Design Group*\n""👋 Ласкаво просимо!\n\n"
        "Для реєстрації напиши своє *Ім'я та Прізвище*:\n"
        "_Наприклад: Іван Петров_",
        parse_mode="Markdown"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 Команди:\n\n"
        "/start — реєстрація\n"
        "/checkin — ручна ранкова відмітка\n"
        "/report — ручний вечірній звіт\n"
        "/mystatus — мій статус сьогодні\n"
        "/mysalary — оновити дані про зарплату\n\n"
        "🔑 Команди керівника:\n"
        "/admin — панель управління\n"
        "/today — звіт за сьогодні\n"
        "/export — експорт в Excel\n"
        "/salary — зарплатні дані співробітників"
    )

# ─── УТРЕННЯЯ ОТМЕТКА ─────────────────────────────────────────────────────────

async def send_morning_checkin(context: ContextTypes.DEFAULT_TYPE):
    """Рассылка утренних уведомлений всем сотрудникам"""
    if datetime.now(tz).weekday() >= 5:  # 5=суббота, 6=воскресенье
        return
    employees = db.get_all_employees()
    admin_ids = db.get_admin_ids()
    for emp in employees:
        if emp["telegram_id"] in admin_ids:
            continue
        try:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Я на роботі", callback_data="checkin_yes")],
                [InlineKeyboardButton("🚗 Ще їду", callback_data="checkin_otw")],
                [InlineKeyboardButton("🤒 Хворію / вихідний", callback_data="checkin_absent")],
            ])
            await context.bot.send_message(
                chat_id=emp["telegram_id"],
                text="🌅 *Доброго ранку!*\n\nЯк справи з роботою сьогодні?",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Не удалось отправить утреннее уведомление {emp['name']}: {e}")

async def checkin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "checkin_yes":
        # Просим геолокацию
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton("📍 Надіслати геолокацію", request_location=True)]],
            resize_keyboard=True, one_time_keyboard=True
        )
        await query.edit_message_text("✅ Чудово! Тепер надішли свою геолокацію для підтвердження.")
        await context.bot.send_message(
            chat_id=user_id,
            text="👇 Натисни кнопку нижче:",
            reply_markup=keyboard
        )
        context.user_data["awaiting_location"] = True

    elif data == "checkin_otw":
        db.save_checkin(user_id, "on_the_way", None, None, None)
        await query.edit_message_text("🚗 Зрозумів, фіксую що ти в дорозі. Щасливої дороги!")
        context.job_queue.run_once(ask_again_otw, when=900, chat_id=user_id, user_id=user_id)

    elif data == "checkin_absent":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🤒 Хворію", callback_data="absent_sick")],
            [InlineKeyboardButton("🌴 Вихідний/Відпустка", callback_data="absent_day_off")],
            [InlineKeyboardButton("🏠 Працюю вдома", callback_data="absent_remote")],
        ])
        await query.edit_message_text("Уточни причину:", reply_markup=keyboard)

    elif data in ("absent_sick", "absent_day_off", "absent_remote"):
        reasons = {
            "absent_sick": ("sick", "🤒 Одужуй швидше!"),
            "absent_day_off": ("day_off", "🌴 Гарного відпочинку!"),
            "absent_remote": ("remote", "🏠 Працюємо дистанційно, зрозумів!"),
        }
        status, msg = reasons[data]
        db.save_checkin(user_id, status, None, None, None)
        await query.edit_message_text(msg)

async def ask_again_otw(context: ContextTypes.DEFAULT_TYPE):
    """Повторный вопрос через 15 минут если сотрудник был в пути"""
    user_id = context.job.user_id
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Я на роботі", callback_data="checkin_yes")],
        [InlineKeyboardButton("🚗 Ще їду", callback_data="checkin_otw")],
        [InlineKeyboardButton("🤒 Хворію / вихідний", callback_data="checkin_absent")],
    ])
    await context.bot.send_message(
        chat_id=user_id,
        text="⏰ Минуло 15 хвилин — ти вже дістався до роботи?",
        reply_markup=keyboard
    )

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка геолокации сотрудника"""
    if not context.user_data.get("awaiting_location"):
        return

    user_id = update.effective_user.id
    loc = update.message.location
    lat, lon = loc.latitude, loc.longitude

    # Считаем расстояние до офиса
    distance = haversine(lat, lon, OFFICE_LAT, OFFICE_LON)
    at_office = distance <= OFFICE_RADIUS_M

    status = "at_office" if at_office else "at_work_remote_loc"
    db.save_checkin(user_id, status, lat, lon, distance)
    context.user_data["awaiting_location"] = False

    if at_office:
        msg = f"✅ Ти в офісі! Відстань до офісу: {int(distance)} м.\nХорошего рабочего дня! 💪"
    else:
        msg = f"📍 Геолокация зафиксирована.\nДо офиса: {int(distance)} м.\n_(Ты не в зоне офиса)_"

    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())

def haversine(lat1, lon1, lat2, lon2):
    """Расстояние между двумя точками в метрах"""
    import math
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

# ─── ВЕЧЕРНИЙ ОТЧЁТ ───────────────────────────────────────────────────────────

async def send_evening_report(context: ContextTypes.DEFAULT_TYPE):
    """Рассылка вечерних уведомлений"""
    if datetime.now(tz).weekday() >= 5:  # 5=суббота, 6=воскресенье
        return
    employees = db.get_all_employees()
    for emp in employees:
        try:
            await context.bot.send_message(
                chat_id=emp["telegram_id"],
                text=(
                    "🌆 *Кінець робочого дня!*\n\n"
                    "Напиши коротко, що зробив сьогодні:\n\n"
                    "_Например: Закончил визуализацию гостиной для клиента Иванов, "
                    "сделал 3D чертёж кухни, провёл встречу с заказчиком_"
                ),
                parse_mode="Markdown"
            )
            context.user_data[f"report_{emp['telegram_id']}"] = True
        except Exception as e:
            logger.error(f"Ошибка вечернего уведомления {emp['name']}: {e}")

async def handle_report_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняем текстовый отчёт сотрудника"""
    user_id = update.effective_user.id
    text = update.message.text

    # Игнорируем команды
    if text.startswith("/"):
        return

    # Регистрация — ввод имени и фамилии
    if context.user_data.get("registering"):
        parts = text.strip().split()
        if len(parts) < 2:
            await update.message.reply_text(
                "⚠️ Напиши *Ім'я та Прізвище* через пробіл:\n"
                "_Наприклад: Іван Петров_",
                parse_mode="Markdown"
            )
            return
        full_name = text.strip()
        user = update.effective_user
        db.register_employee(user.id, full_name, user.username or "")
        context.user_data["registering"] = False
        context.user_data["salary_step"] = "date"
        context.user_data["full_name"] = full_name
        await update.message.reply_text(
            f"✅ *{full_name}, ты успешно зарегистрирован!* 🎉\n\n"
            "👋 Ласкаво просимо до команди *SPILNO Design Group!*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Тепер заповнимо твої робочі дані 📋\n\n"
            "📅 *Крок 1 з 3 — Дата зарплати*\n"
            "Напиши дату виплати зарплати:\n"
            "_Наприклад: 5 і 20 кожного місяця_",
            parse_mode="Markdown"
        )
        return

    # Шаги заполнения зарплатных данных
    if context.user_data.get("salary_step") == "date":
        context.user_data["salary_date"] = text.strip()
        context.user_data["salary_step"] = "amount"
        await update.message.reply_text(
            "💵 *Крок 2 з 3 — Ставка*\n"
            "Напиши свій оклад (фіксована сума):\n"
            "_Наприклад: 1500🪙_",
            parse_mode="Markdown"
        )
        return

    if context.user_data.get("salary_step") == "amount":
        context.user_data["salary_amount"] = text.strip()
        context.user_data["salary_step"] = "bonus"
        await update.message.reply_text(
            "🎁 *Крок 3 з 3 — Бонуси*\n"
            "Напиши суму своїх бонусів (фіксована сума):\n"
            "_Наприклад: 200🪙, або 'без бонусів'_",
            parse_mode="Markdown"
        )
        return

    if context.user_data.get("salary_step") == "bonus":
        bonus_info = text.strip()
        salary_date = context.user_data.get("salary_date", "")
        salary_amount = context.user_data.get("salary_amount", "")
        db.save_salary_info(user_id, salary_date, salary_amount, bonus_info)
        context.user_data["salary_step"] = None
        await update.message.reply_text(
            "🎊 *Реєстрація завершена успішно!*\n"
            "✅ Дякуємо, всі дані прийнято!\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "📋 *Твої дані:*\n"
            "📅 Дата зарплати: " + salary_date + "\n"
            "💵 Ставка: " + salary_amount + "\n"
            "🎁 Бонуси: " + bonus_info + "\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 *Як я буду працювати:*\n"
            "🌅 О 10:00 — запитаю чи ти на роботі?\n"
            "⏰ О 11:00 — нагадаю якщо не відповів\n"
            "🌆 О 18:00 — попрошу звіт за день\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🏢 *SPILNO Design Group*\n"
            "💪 Гарного робочого дня!\n"
            "🙌 Раді бачити тебе в команді!",
            parse_mode="Markdown"
        )
        return

    # Подтверждение получения зарплаты
    if context.user_data.get("awaiting_salary_confirm"):
        status = context.user_data.pop("awaiting_salary_confirm")
        icons = {"yes": "✅", "partial": "⚠️", "no": "❌"}
        icon = icons.get(status, "💰")

        # Уведомляем администраторов
        user_name = update.effective_user.full_name
        admin_text = icon + " *Звіт про зарплату*\n━━━━━━━━━━━━━━━━━━━━━━\n👤 Співробітник: " + user_name + "\n💬 Повідомлення: " + text + "\n🏢 *SPILNO Design Group*"
        for admin_id in db.get_admin_ids():
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=admin_text,
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления админа о зарплате: {e}")

        await update.message.reply_text(
            icon + " *Спасибо, данные записаны!*\n\n💬 " + text + "\n\n🏢 *SPILNO Design Group*",
            parse_mode="Markdown"
        )
        return

    # Принимаем отчёт после 15:00
    now = datetime.now(tz)
    if now.hour >= 14:
        db.save_report(user_id, text)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Вірно", callback_data="report_ok")],
            [InlineKeyboardButton("✏️ Змінити", callback_data="report_edit")],
        ])
        await update.message.reply_text(
            f"📝 Звіт збережено!\n\n_{text}_\n\nВсе вірно?",
            parse_mode="Markdown",
            reply_markup=keyboard
        )

async def report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "report_ok":
        await query.edit_message_text("✅ Звіт підтверджено. Гарного вечора! 🌙")
    elif query.data == "report_edit":
        await query.edit_message_text("✏️ Напиши звіт заново:")

# ─── РУЧНЫЕ КОМАНДЫ ───────────────────────────────────────────────────────────

async def manual_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Я на роботі", callback_data="checkin_yes")],
        [InlineKeyboardButton("🚗 Ще їду", callback_data="checkin_otw")],
        [InlineKeyboardButton("🤒 Хворію / вихідний", callback_data="checkin_absent")],
    ])
    await update.message.reply_text("Відміть свій статус:", reply_markup=keyboard)

async def manual_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📝 Напиши свій звіт за сьогодні:\n\n"
        "_Що зробив, над чим працював, що залишилось_",
        parse_mode="Markdown"
    )

async def my_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    status = db.get_today_status(user_id)
    if not status:
        await update.message.reply_text("Сьогодні ти ще не відмічався. Використовуй /checkin")
        return
    
    status_labels = {
        "at_office": "✅ В офісі",
        "on_the_way": "🚗 В дорозі",
        "sick": "🤒 Хворіє",
        "day_off": "🌴 Вихідний",
        "remote": "🏠 Дистанційно",
        "at_work_remote_loc": "📍 Работает (не в офисе)",
    }
    
    checkin_label = status_labels.get(status.get("checkin_status", ""), "—")
    report_text = status.get("report_text", "Не здано")
    
    await update.message.reply_text(
        f"📊 *Твій статус сьогодні:*\n\n"
        f"Прихід: {checkin_label}\n"
        f"Звіт: {report_text[:200] if report_text else '❌ Не здано'}",
        parse_mode="Markdown"
    )

# ─── ПАНЕЛЬ РУКОВОДИТЕЛЯ ──────────────────────────────────────────────────────

ADMIN_IDS = []  # Заполняется из config.py

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in db.get_admin_ids():
        await update.message.reply_text("⛔ Доступ заборонено.")
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Зведення сьогодні", callback_data="admin_today")],
        [InlineKeyboardButton("📍 Геолокації", callback_data="admin_locations")],
        [InlineKeyboardButton("📝 Звіти", callback_data="admin_reports")],
        [InlineKeyboardButton("💰 Зарплати співробітників", callback_data="admin_salary")],
        [InlineKeyboardButton("⬇️ Експорт Excel", callback_data="admin_export")],
        [InlineKeyboardButton("👥 Співробітники", callback_data="admin_employees")],
    ])
    await update.message.reply_text("🔑 *Панель керівника*", parse_mode="Markdown", reply_markup=keyboard)

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in db.get_admin_ids():
        await query.edit_message_text("⛔ Доступ заборонено.")
        return

    if query.data == "admin_today":
        summary = db.get_today_summary()
        text = "📊 *Зведення за сьогодні:*\n\n"
        text += f"✅ В офісі: {summary['at_office']}\n"
        text += f"🏠 Дистанційно: {summary['remote']}\n"
        text += f"🚗 В дорозі: {summary['on_the_way']}\n"
        text += f"🤒 Хворіють/вихідний: {summary['absent']}\n"
        text += f"❓ Не відповіли: {summary['no_response']}\n"
        text += f"📝 Здали звіт: {summary['reports_submitted']}\n"
        text += f"📋 Не здали звіт: {summary['reports_missing']}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="admin_back")]])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)

    elif query.data == "admin_locations":
        employees = db.get_today_locations()
        text = "📍 *Геолокації сьогодні:*\n\n"
        for emp in employees:
            if emp["distance"] is not None:
                icon = "✅" if emp["distance"] <= OFFICE_RADIUS_M else "🔴"
                text += f"{icon} {emp['name']} — {int(emp['distance'])} м від офісу\n"
            else:
                text += f"❓ {emp['name']} — геолокацію не надіслано\n"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="admin_back")]])
        await query.edit_message_text(text or "Нет данных", parse_mode="Markdown", reply_markup=keyboard)

    elif query.data == "admin_reports":
        reports = db.get_today_reports()
        text = "📝 *Звіти за сьогодні:*\n\n"
        for r in reports:
            if r["report_text"]:
                text += f"👤 *{r['name']}:*\n{r['report_text'][:200]}\n\n"
            else:
                text += f"❌ *{r['name']}* — не здав звіт\n\n"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="admin_back")]])
        await query.edit_message_text(text[:4000] or "Нет данных", parse_mode="Markdown", reply_markup=keyboard)

    elif query.data == "admin_export":
        await query.edit_message_text("⏳ Генерую Excel файл...")
        from export import generate_excel
        filepath = generate_excel(db)
        await context.bot.send_document(
            chat_id=user_id,
            document=open(filepath, "rb"),
            filename=f"report_{datetime.now().strftime('%Y-%m-%d')}.xlsx",
            caption="📊 Звіт за останні 30 днів"
        )
        await query.edit_message_text("✅ Excel файл надіслано!")

    elif query.data == "admin_employees":
        employees = db.get_all_employees()
        text = "👥 *Всі співробітники:*\n\n"
        for i, emp in enumerate(employees, 1):
            admin_mark = " 👑" if emp["is_admin"] else ""
            text += f"{i}. {emp['name']}{admin_mark} (@{emp['username'] or '—'})\n"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="admin_back")]])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)

    elif query.data == "admin_salary":
        import re
        data = db.get_all_salary_info()
        text = "💰 *Зарплати співробітників:*\n"
        text += "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        total_all = 0
        for emp in data:
            salary = emp.get("salary_amount", "—")
            bonus = emp.get("bonus_info", "—")
            salary_date = emp.get("salary_date", "—")
            s_nums = re.findall(r"[\d.]+", str(salary))
            b_nums = re.findall(r"[\d.]+", str(bonus))
            try:
                total = float(s_nums[0]) + (float(b_nums[0]) if b_nums else 0)
                total_str = f"{total:,.0f}🪙"
                total_all += total
            except:
                total_str = "—"
            text += f"👤 *{emp['name']}*\n"
            text += f"📅 Дата виплати: {salary_date}\n"
            text += f"💵 Ставка: {salary}\n"
            text += f"🎁 Бонуси: {bonus}\n"
            text += f"💳 До виплати: *{total_str}*\n"
            text += "─────────────────────\n"
        text += f"\n💼 *Разом до виплати всім: {total_all:,.0f}🪙*\n"
        text += "🏢 *SPILNO Design Group*"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="admin_back")]])
        await query.edit_message_text(text[:4000], parse_mode="Markdown", reply_markup=keyboard)

    elif query.data == "admin_back":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Зведення сьогодні", callback_data="admin_today")],
            [InlineKeyboardButton("📍 Геолокації", callback_data="admin_locations")],
            [InlineKeyboardButton("📝 Звіти", callback_data="admin_reports")],
            [InlineKeyboardButton("💰 Зарплати співробітників", callback_data="admin_salary")],
            [InlineKeyboardButton("⬇️ Експорт Excel", callback_data="admin_export")],
            [InlineKeyboardButton("👥 Співробітники", callback_data="admin_employees")],
        ])
        await query.edit_message_text("🔑 *Панель керівника*", parse_mode="Markdown", reply_markup=keyboard)

async def my_salary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    context.user_data["salary_step"] = "date"
    context.user_data["full_name"] = update.effective_user.full_name
    salary = db.get_salary_info(user_id)

    if salary:
        current = (
            "📋 *Поточні дані:*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📅 Дата: " + str(salary.get("salary_date", "—")) + "\n"
            "💵 Ставка: " + str(salary.get("salary_amount", "—")) + "\n"
            "🎁 Бонуси: " + str(salary.get("bonus_info", "—")) + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        )
    else:
        current = ""

    await update.message.reply_text(
        "✏️ *Оновлення даних про зарплату*\n\n" +
        current +
        "📅 *Крок 1 з 3 — Дата зарплати*\n"
        "Напиши дату виплати зарплати:\n"
        "_Наприклад: 5 і 20 кожного місяця_",
        parse_mode="Markdown"
    )

async def salary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in db.get_admin_ids():
        await update.message.reply_text("⛔ Доступ заборонено.")
        return
    data = db.get_all_salary_info()
    if not data:
        await update.message.reply_text("Немає даних про зарплати.")
        return
    text = "💰 *Зарплатні дані співробітників:*\n"
    text += "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for emp in data:
        text += f"👤 *{emp['name']}*\n"
        text += f"📅 Дата виплати: {emp['salary_date'] or '—'}\n"
        text += f"💵 Ставка: {emp['salary_amount'] or '—'}\n"
        text += f"🎁 Бонуси: {emp['bonus_info'] or '—'}\n"
        text += "─────────────────────\n"
    await update.message.reply_text(text[:4000], parse_mode="Markdown")

async def notify_salary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in db.get_admin_ids():
        await update.message.reply_text("⛔ Доступ заборонено.")
        return
    employees = db.get_all_employees()
    admin_ids = db.get_admin_ids()
    count = 0
    for emp in employees:
        if emp["telegram_id"] in admin_ids:
            continue
        salary = db.get_salary_info(emp["telegram_id"])
        if salary:
            continue  # уже заполнил
        try:
            await context.bot.send_message(
                chat_id=emp["telegram_id"],
                text=(
                    "📋 *Шановний співробітник!*\n\n"
                    "Прохання заповнити зарплатну відомість прямо зараз.\n\n"
                    "Це займе лише 1 хвилину 👇\n\n"
                    "Напиши команду /mysalary і заповни 3 кроки:\n"
                    "📅 Дата зарплати\n"
                    "💵 Ставка\n"
                    "🎁 Бонуси\n\n"
                    "🏢 *SPILNO Design Group*"
                ),
                parse_mode="Markdown"
            )
            count += 1
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления {emp['name']}: {e}")
    await update.message.reply_text(
        f"✅ Повідомлення надіслано {count} співробітникам у яких не заповнена відомість!"
    )

async def commands_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in db.get_admin_ids():
        await update.message.reply_text("⛔ Доступ заборонено.")
        return
    text = (
        "📖 *Всі команди SPILNO Design Group*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔑 *Панель управління:*\n"
        "/admin — панель з кнопками\n"
        "/today — зведення за сьогодні\n"
        "/salary — зарплати співробітників\n"
        "/export — завантажити Excel за 30 днів\n"
        "/commands — цей список команд\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 *В панелі /admin доступно:*\n"
        "📊 Зведення — хто на роботі сьогодні\n"
        "📍 Геолокації — расстояние до офиса\n"
        "📝 Звіти — что сделал каждый\n"
        "💰 Зарплати — ставки і бонуси\n"
        "⬇️ Експорт Excel — отчёт за 30 дней\n"
        "👥 Співробітники — список всех\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "👤 *Команди співробітників:*\n"
        "/start — реєстрація\n"
        "/checkin — відмітитись вручну\n"
        "/report — здати звіт вручну\n"
        "/mystatus — мій статус сьогодні\n"
        "/mysalary — оновити дані про зарплату\n"
        "/help — список команд\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "⏰ *Автоматичні сповіщення:*\n"
        "🌅 10:00 — відмітка про прихід\n"
        "⏰ 11:00 — нагадування тим хто не відповів\n"
        "🌆 18:00 — вечірній звіт\n"
        "💰 10:00 — нагадування про зарплату (за 3 дні)\n"
        "💳 19:00 — підтвердження отримання зарплати\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🏢 *SPILNO Design Group*"
    )
    msg = await update.message.reply_text(text, parse_mode="Markdown")
    await context.bot.pin_chat_message(
        chat_id=update.effective_chat.id,
        message_id=msg.message_id,
        disable_notification=True
    )

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in db.get_admin_ids():
        await update.message.reply_text("⛔ Доступ заборонено.")
        return
    summary = db.get_today_summary()
    text = (
        f"📊 *Зведення за {datetime.now(tz).strftime('%d.%m.%Y')}:*\n\n"
        f"✅ В офісі: {summary['at_office']}\n"
        f"🏠 Дистанційно: {summary['remote']}\n"
        f"🚗 В дорозі: {summary['on_the_way']}\n"
        f"🤒 Хворіють/вихідний: {summary['absent']}\n"
        f"❓ Не відповіли: {summary['no_response']}\n"
        f"📝 Здали звіт: {summary['reports_submitted']}\n"
        f"📋 Не здали звіт: {summary['reports_missing']}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in db.get_admin_ids():
        await update.message.reply_text("⛔ Доступ заборонено.")
        return
    await update.message.reply_text("⏳ Генерую Excel...")
    from export import generate_excel
    filepath = generate_excel(db)
    await update.message.reply_document(
        document=open(filepath, "rb"),
        filename=f"report_{datetime.now().strftime('%Y-%m-%d')}.xlsx",
        caption="📊 Звіт за останні 30 днів"
    )

# ─── НАПОМИНАНИЕ НЕ ОТВЕТИВШИМ ────────────────────────────────────────────────

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Напоминание тем, кто не ответил на утреннюю отметку"""
    if datetime.now(tz).weekday() >= 5:  # 5=суббота, 6=воскресенье
        return
    no_response = db.get_no_response_employees()
    for emp in no_response:
        try:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Я на роботі", callback_data="checkin_yes")],
                [InlineKeyboardButton("🚗 Ще їду", callback_data="checkin_otw")],
                [InlineKeyboardButton("🤒 Хворію / вихідний", callback_data="checkin_absent")],
            ])
            await context.bot.send_message(
                chat_id=emp["telegram_id"],
                text="⏰ Нагадування! Ти ще не відмітився сьогодні. Все гаразд?",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Ошибка напоминания для {emp['name']}: {e}")

# ─── НАПОМИНАНИЕ О ЗАРПЛАТЕ ──────────────────────────────────────────────────

async def check_salary_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Проверяет у кого через 3 дня зарплата и уведомляет руководителя"""
    from datetime import date, timedelta
    import calendar

    today = date.today()
    in_3_days = today + timedelta(days=3)
    target_day = in_3_days.day

    all_salary = db.get_all_salary_info()
    reminders = []

    for emp in all_salary:
        if not emp.get("salary_date"):
            continue
        # Ищем числа в строке даты
        import re
        days_found = re.findall(r"\d+", str(emp["salary_date"]))
        for d in days_found:
            if int(d) == target_day:
                reminders.append(emp)
                break

    if not reminders:
        return

    text = "💰 *Нагадування про зарплату!*\n"
    text += f"📅 Через 3 дні — {in_3_days.strftime('%d.%m.%Y')}\n"
    text += "━━━━━━━━━━━━━━━━━━━━━━\n\n"

    for emp in reminders:
        salary = emp.get("salary_amount", "—")
        bonus = emp.get("bonus_info", "—")

        # Считаем итого если возможно
        import re
        s_nums = re.findall(r"[\d.]+", str(salary))
        b_nums = re.findall(r"[\d.]+", str(bonus))
        try:
            total = float(s_nums[0]) + (float(b_nums[0]) if b_nums else 0)
            total_str = f"{total:,.0f}🪙"
        except:
            total_str = "уточни вручную"

        text += f"👤 *{emp['name']}*\n"
        text += f"💵 Ставка: {salary}\n"
        text += f"🎁 Бонуси: {bonus}\n"
        text += f"💳 До виплати: *{total_str}*\n"
        text += "─────────────────────\n"

    text += "\n🏢 *SPILNO Design Group*"

    # Отправляем всем администраторам
    for admin_id in db.get_admin_ids():
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Ошибка отправки напоминания о зарплате: {e}")

# ─── ПРОВЕРКА ПОЛУЧЕНИЯ ЗАРПЛАТЫ ─────────────────────────────────────────────

async def check_salary_day(context: ContextTypes.DEFAULT_TYPE):
    """В день зарплаты — уведомляет директора утром и спрашивает сотрудника вечером"""
    from datetime import date
    import re

    today = date.today()
    target_day = today.day

    all_salary = db.get_all_salary_info()
    payday_employees = []

    for emp in all_salary:
        if not emp.get("salary_date"):
            continue
        days_found = re.findall(r"\d+", str(emp["salary_date"]))
        for d in days_found:
            if int(d) == target_day:
                payday_employees.append(emp)
                break

    # Уведомляем директора утром
    if payday_employees:
        text = "💰 *Сьогодні день зарплати!*\n"
        text += f"📅 {today.strftime('%d.%m.%Y')}\n"
        text += "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        text += "💳 *Потрібно виплатити:*\n\n"
        total_all = 0
        for emp in payday_employees:
            salary = emp.get("salary_amount", "—")
            bonus = emp.get("bonus_info", "—")
            s_nums = re.findall(r"[\d.]+", str(salary))
            b_nums = re.findall(r"[\d.]+", str(bonus))
            try:
                total = float(s_nums[0]) + (float(b_nums[0]) if b_nums else 0)
                total_str = f"{total:,.0f}🪙"
                total_all += total
            except:
                total_str = "—"
            text += f"👤 *{emp['name']}*\n"
            text += f"💵 Ставка: {salary}\n"
            text += f"🎁 Бонуси: {bonus}\n"
            text += f"💳 До виплати: *{total_str}*\n"
            text += "─────────────────────\n"
        text += f"\n💼 *Разом сьогодні: {total_all:,.0f}🪙*\n"
        text += "🏢 *SPILNO Design Group*"

        for admin_id in db.get_admin_ids():
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=text,
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления директора о зарплате: {e}")

    for emp in payday_employees:
        if True:
            if True:
                try:
                    salary = emp.get("salary_amount", "—")
                    bonus = emp.get("bonus_info", "—")
                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ Так, отримав все", callback_data="salary_received_yes")],
                        [InlineKeyboardButton("⚠️ Отримав частково", callback_data="salary_received_partial")],
                        [InlineKeyboardButton("❌ Не отримав", callback_data="salary_received_no")],
                    ])
                    # Find telegram_id for this employee
                    emp_row = context.application.bot_data.get("db_conn") 
                    rows = db.conn.execute(
                        "SELECT telegram_id FROM employees WHERE name = ?", (emp["name"],)
                    ).fetchone()
                    if not rows:
                        continue
                    telegram_id = rows["telegram_id"]
                    await context.bot.send_message(
                        chat_id=telegram_id,
                        text="💰 *Сьогодні день зарплати!*\n━━━━━━━━━━━━━━━━━━━━━━\n\n💵 Очікувана ставка: " + str(salary) + "\n🎁 Очікувані бонуси: " + str(bonus) + "\n\nТи отримав зарплату сьогодні?",
                        parse_mode="Markdown",
                        reply_markup=keyboard
                    )
                    # Save state for follow-up
                    context.bot_data[f"salary_check_{telegram_id}"] = {
                        "name": emp["name"],
                        "expected_salary": salary,
                        "expected_bonus": bonus,
                        "telegram_id": telegram_id
                    }
                except Exception as e:
                    logger.error(f"Ошибка проверки зарплаты {emp['name']}: {e}")
                break

async def salary_received_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "salary_received_yes":
        await query.edit_message_text(
            "✅ *Чудово! Раді чути!*\n\nНапиши точну суму яку отримав:\n_Наприклад: 1200🪙 зарплата + 150🪙 бонус_",
            parse_mode="Markdown"
        )
        context.user_data["awaiting_salary_confirm"] = "yes"

    elif data == "salary_received_partial":
        await query.edit_message_text(
            "⚠️ *Отримав частково.*\n\nНапиши скільки отримав і що не доплатили:\n_Наприклад: отримав 800🪙 з 1200🪙, бонус не виплатили_",
            parse_mode="Markdown"
        )
        context.user_data["awaiting_salary_confirm"] = "partial"

    elif data == "salary_received_no":
        await query.edit_message_text(
            "❌ *Зарплату не отримано.*\n\nНапиши детальніше що сталося:\n_Наприклад: сказали перенесуть на завтра_",
            parse_mode="Markdown"
        )
        context.user_data["awaiting_salary_confirm"] = "no"

# ─── ЗАПУСК ───────────────────────────────────────────────────────────────────

def main():
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = Application.builder().token(BOT_TOKEN).build()

    # Обработчики команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("checkin", manual_checkin))
    app.add_handler(CommandHandler("report", manual_report))
    app.add_handler(CommandHandler("mystatus", my_status))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("salary", salary_cmd))
    app.add_handler(CommandHandler("commands", commands_cmd))
    app.add_handler(CommandHandler("notify", notify_salary_cmd))
    app.add_handler(CommandHandler("mysalary", my_salary_cmd))

    # Обработчики кнопок
    app.add_handler(CallbackQueryHandler(checkin_callback, pattern="^checkin_|^absent_"))
    app.add_handler(CallbackQueryHandler(report_callback, pattern="^report_"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))
    app.add_handler(CallbackQueryHandler(salary_received_callback, pattern="^salary_received_"))

    # Геолокация и текст
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_report_text))

    # Расписание
    job_queue = app.job_queue
    job_queue.run_daily(send_morning_checkin,     time=time(10, 0, tzinfo=tz))  # 10:00
    job_queue.run_daily(send_reminder,            time=time(11, 0, tzinfo=tz))  # 11:00 напоминание
    job_queue.run_daily(send_evening_report,      time=time(18, 0, tzinfo=tz))  # 18:00
    job_queue.run_daily(check_salary_reminders,   time=time(10, 0, tzinfo=tz))  # 10:00 напоминание за 3 дня
    job_queue.run_daily(check_salary_day,         time=time(19, 0, tzinfo=tz))  # 19:00 день зарплаты

    logger.info("Бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
