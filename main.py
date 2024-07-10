import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, CallbackContext, ApplicationBuilder, \
    ContextTypes
from datetime import datetime, timedelta
import pytz

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

SCOUTS = {}
USER_NAMES = {}
USER_STATES = {}
SHIFT_START_TIMES = {}
MIN_WORKING_HOURS = {}
OWNER_ID = None

MOSCOW_TZ = pytz.timezone('Europe/Moscow')

EXCLUDED_MESSAGES = [
    "❌ Вы не на перерыве. ❌",
    "❌ Ошибка подтверждения завершения смены. Пользователь не на смене.",
    "❌ У вас нет возможности взять перерыв. ❌",
    "❌ Вы уже на перерыве. ❌",
    "❌ У вас нет доступа к этому функционалу."
]


async def start(update: Update, context: CallbackContext) -> None:
    user_id = update.message.from_user.id
    if user_id in SCOUTS or user_id == OWNER_ID:
        user_state = USER_STATES.get(user_id, 'idle')
        keyboard = []
        if user_state == 'idle':
            keyboard.append([InlineKeyboardButton("🔋 Начать смену", callback_data='start_shift')])
        elif user_state == 'on_shift':
            keyboard.append([InlineKeyboardButton("🪫 Закончить смену", callback_data='end_shift')])
            if user_id in SCOUTS:
                if context.user_data.get('on_break', False):
                    keyboard.append([InlineKeyboardButton("☕ Закончить перерыв", callback_data='end_break')])
                else:
                    keyboard.append([InlineKeyboardButton("☕ Взять перерыв", callback_data='take_break')])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text('✅ Выберите действие:', reply_markup=reply_markup)
    else:
        await update.message.reply_text("❌ У вас нет доступа к этому функционалу.")


async def button(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    # Проверка доступа пользователя
    if user_id not in SCOUTS and user_id != OWNER_ID:
        await query.edit_message_text(text="❌ У вас нет доступа к этому функционалу.")
        return

    user_full_name = USER_NAMES.get(user_id, 'User')
    role = 'Скаут' if user_id in SCOUTS else 'Старший Скаут'
    now = datetime.now(MOSCOW_TZ)

    if query.data == 'start_shift':
        context.user_data['total_break_time'] = 0
        USER_STATES[user_id] = 'on_shift'
        SHIFT_START_TIMES[user_id] = now
        message = f"🛴 {role} {user_full_name}. \nСмена #1 работу начал."
    elif query.data == 'end_shift':
        shift_duration = now - SHIFT_START_TIMES.get(user_id, now)
        required_shift_duration = timedelta(
            hours=MIN_WORKING_HOURS.get(user_id, 12))  # По умолчанию 12 часов, если не установлено

        if shift_duration >= required_shift_duration:
            USER_STATES[user_id] = 'idle'
            context.user_data['on_break'] = False
            context.user_data.pop('break_start_time', None)
            context.user_data.pop('total_break_time', None)
            message = f"🛴 {role} {user_full_name}. \nСмена #1 работу закончил."
        else:
            await query.edit_message_text(
                text=f"❌ Вы не можете закончить смену, так как не прошло достаточно времени. \n✉️ Запрос отправлен старшему скауту для подтверждения."
            )
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=f"🔔 Запрос на досрочное завершение смены от {user_full_name}. \n⏳ Прошедшее время: {shift_duration}. \nПодтвердите или отклоните запрос.",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Подтвердить", callback_data=f'approve_end_shift_{user_id}'),
                        InlineKeyboardButton("❌ Отклонить", callback_data=f'deny_end_shift_{user_id}')
                    ]
                ])
            )
            return
    elif query.data == 'take_break':
        if user_id in SCOUTS:
            if context.user_data.get('on_break', False):
                await query.edit_message_text("❌ Вы уже на перерыве. ❌")
            else:
                total_break_time = context.user_data.get('total_break_time', 0)
                if total_break_time < SCOUTS[user_id]:
                    context.user_data['break_start_time'] = now
                    context.user_data['on_break'] = True
                    message = f"☕️ Скаут {user_full_name} взял перерыв."

                    context.job_queue.run_repeating(check_break_time, interval=10, first=60,
                                                    data={'user_id': user_id, 'user_full_name': user_full_name,
                                                          'start_time': now}, name=f'check_break_time_{user_id}')
                else:
                    await query.edit_message_text(
                        "❌ Вы не можете взять перерыв, так как исчерпано общее время перерыва.")
        else:
            await query.edit_message_text("❌ У вас нет возможности взять перерыв. ❌")
    elif query.data == 'end_break':
        if user_id in SCOUTS and context.user_data.get('on_break', False):
            start_time = context.user_data.get('break_start_time', now)
            time_on_break = (now - start_time).total_seconds() / 60
            total_break_time = context.user_data.get('total_break_time', 0) + time_on_break
            if total_break_time <= SCOUTS[user_id]:
                context.user_data['on_break'] = False
                context.user_data['total_break_time'] = total_break_time
                remaining_time = SCOUTS[user_id] - total_break_time
                message = f"☕️ Скаут {user_full_name} перерыв закончил. \n⏳ Время на перерыве: {int(time_on_break)} минут. \n⌛️ Оставшееся время на перерыв: {int(remaining_time)} минут."

                jobs = context.job_queue.get_jobs_by_name(f'check_break_time_{user_id}')
                for job in jobs:
                    job.schedule_removal()
            else:
                context.user_data['on_break'] = False
                context.user_data['total_break_time'] = SCOUTS[user_id]
                message = f"☕️ Скаут {user_full_name} перерыв закончил. \n⏳ Время на перерыве: {int(SCOUTS[user_id] - (total_break_time - time_on_break))} минут. \n❌ Превышено общее время перерыва на смену."

                jobs = context.job_queue.get_jobs_by_name(f'check_break_time_{user_id}')
                for job in jobs:
                    job.schedule_removal()
        else:
            await query.edit_message_text("❌ Вы не на перерыве. ❌")
    elif query.data.startswith('approve_end_shift_'):
        approved_user_id = int(query.data.split('_')[-1])
        if approved_user_id in USER_STATES and USER_STATES[approved_user_id] == 'on_shift':
            USER_STATES[approved_user_id] = 'idle'
            context.user_data['on_break'] = False
            context.user_data.pop('break_start_time', None)
            context.user_data.pop('total_break_time', None)
            message = f"🛴 Скаут {USER_NAMES[approved_user_id]}. \nСмена #1 работу закончил по подтверждению старшего скаута."
            await context.bot.send_message(chat_id=approved_user_id, text=message)
        else:
            message = "❌ Ошибка подтверждения завершения смены. Пользователь не на смене."
    elif query.data.startswith('deny_end_shift_'):
        denied_user_id = int(query.data.split('_')[-1])
        message = f"❌ Запрос на досрочное завершение смены от {USER_NAMES[denied_user_id]} отклонен старшим скаутом."
        await context.bot.send_message(chat_id=denied_user_id, text=message)

    await query.edit_message_text(text=message)

    await send_options(update, context)
    if message not in EXCLUDED_MESSAGES:
        await context.bot.send_message(chat_id='id беседы с минусом', text=message)
    await context.bot.send_message(chat_id=OWNER_ID, text=message)


async def check_break_time(context: CallbackContext) -> None:
    job = context.job
    user_id = job.data['user_id']
    user_full_name = job.data['user_full_name']
    start_time = job.data['start_time']
    now = datetime.now(MOSCOW_TZ)
    time_on_break = (now - start_time).total_seconds() / 60
    total_break_time = context.user_data.get('total_break_time', 0) + time_on_break

    if total_break_time >= SCOUTS[user_id]:
        context.user_data['on_break'] = False
        job.schedule_removal()

        message = f"☕️ Скаут {user_full_name} перерыв закончил. \n⏳ Время на перерыве: {int(total_break_time)} минут. \n❌ Превышено общее время перерыва на смену."
        context.user_data['total_break_time'] = SCOUTS[user_id]
        await context.bot.send_message(chat_id=user_id, text=message)


async def send_options(update: Update, context: CallbackContext) -> None:
    user_id = update.callback_query.from_user.id
    user_state = USER_STATES.get(user_id, 'idle')
    keyboard = []
    if user_state == 'idle':
        keyboard.append([InlineKeyboardButton("🔋 Начать смену", callback_data='start_shift')])
    elif user_state == 'on_shift':
        keyboard.append([InlineKeyboardButton("🪫 Закончить смену", callback_data='end_shift')])
        if user_id in SCOUTS:
            if context.user_data.get('on_break', False):
                keyboard.append([InlineKeyboardButton("☕ Закончить перерыв", callback_data='end_break')])
            else:
                keyboard.append([InlineKeyboardButton("☕ Взять перерыв", callback_data='take_break')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(chat_id=update.callback_query.message.chat_id, text='✅ Выберите действие:',
                                   reply_markup=reply_markup)


async def error(update: Update, context: CallbackContext) -> None:
    logger.warning('Update "%s" caused error "%s"', update, context.error)


def main() -> None:
    application = ApplicationBuilder().token("BOT API KEY").build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button))

    application.add_error_handler(error)

    application.run_polling()


if __name__ == '__main__':
    SCOUTS['id скаута'] = 60 # 60 минут на перерыв
    USER_NAMES['id скаута'] = 'Имя (@тег)'
    MIN_WORKING_HOURS['id скаута'] = 12  # Минимум 12 часов работы

    OWNER_ID = {'id владельца'}
    USER_NAMES['id владельца'] = 'Имя (@тег)'

    main()
