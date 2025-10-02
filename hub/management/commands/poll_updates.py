import json
import time
import requests
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from hub.models import Bot, BotUser, MessageLog


class Command(BaseCommand):
    help = "Long-poll Telegram getUpdates for a bot token and persist users/messages"

    def add_arguments(self, parser):
        parser.add_argument('--bot-token', required=False, help='Telegram bot token')
        parser.add_argument('--bot-id', type=int, required=False, help='Bot ID from DB')
        parser.add_argument('--timeout', type=int, default=50, help='Long poll timeout seconds')
        parser.add_argument('--sleep', type=int, default=1, help='Sleep between polls when no updates')

    def handle(self, *args, **options):
        bot_token = options.get('bot_token')
        bot_id = options.get('bot_id')
        timeout = options.get('timeout')
        sleep_sec = options.get('sleep')

        if not bot_token and not bot_id:
            raise CommandError('Provide --bot-token or --bot-id')

        bot = None
        if bot_id:
            try:
                bot = Bot.objects.get(id=bot_id)
                bot_token = bot.token
            except Bot.DoesNotExist:
                raise CommandError('Bot not found for --bot-id')
        elif bot_token:
            bot = Bot.objects.filter(token=bot_token).first()
            if not bot:
                bot = Bot.objects.create(name='Polled Bot', token=bot_token, is_active=True)

        self.stdout.write(self.style.SUCCESS(f"Polling updates for bot: {bot.name}"))

        offset = None
        while True:
            try:
                params = {'timeout': timeout}
                if offset:
                    params['offset'] = offset
                r = requests.get(f"https://api.telegram.org/bot{bot_token}/getUpdates", params=params, timeout=timeout+5)
                js = r.json()
            except Exception as ex:
                self.stderr.write(f"Error fetching updates: {ex}")
                time.sleep(sleep_sec)
                continue

            if not js.get('ok'):
                self.stderr.write(f"Telegram returned error: {js}")
                time.sleep(sleep_sec)
                continue

            for upd in js.get('result', []):
                offset = upd['update_id'] + 1

                # Handle callback queries first (button presses)
                callback_query = upd.get('callback_query')
                if callback_query:
                    cq_from = callback_query.get('from') or {}
                    cq_message = callback_query.get('message') or {}
                    cq_chat = (cq_message.get('chat') or {})
                    cq_chat_id = cq_chat.get('id') or cq_from.get('id')
                    data = callback_query.get('data') or ''

                    if not cq_chat_id:
                        continue

                    bot_user, _ = BotUser.objects.get_or_create(
                        bot=bot,
                        telegram_id=cq_chat_id,
                        defaults={
                            'username': cq_from.get('username'),
                            'first_name': cq_from.get('first_name'),
                            'last_name': cq_from.get('last_name'),
                            'language_code': cq_from.get('language_code'),
                            'last_seen_at': timezone.now(),
                        }
                    )

                    bot_user.last_seen_at = timezone.now()
                    bot_user.save(update_fields=['last_seen_at'])

                    # Acknowledge callback to avoid loading state on client
                    try:
                        requests.post(
                            f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
                            json={
                                'callback_query_id': callback_query.get('id'),
                                'text': 'Ready! You can send your question now.',
                                'show_alert': False,
                            },
                            timeout=10,
                        )
                    except Exception as ex:
                        self.stderr.write(f"Error answering callback: {ex}")

                    if data == 'enable_questions':
                        bot_user.state = 'enabled'
                        if not bot_user.started_at:
                            bot_user.started_at = timezone.now()
                        bot_user.save(update_fields=['state', 'started_at'] if bot_user.started_at else ['state'])

                        try:
                            requests.post(
                                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                                json={
                                    'chat_id': cq_chat_id,
                                    'text': 'You can now send your question about campaigns/candidates.',
                                },
                                timeout=10,
                            )
                        except Exception as ex:
                            self.stderr.write(f"Error sending enabled message: {ex}")
                    elif data == 'request_contact_btn':
                        # Show a reply keyboard that requests contact
                        try:
                            requests.post(
                                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                                json={
                                    'chat_id': cq_chat_id,
                                    'text': 'Please tap the button below to share your phone number.',
                                    'reply_markup': {
                                        'keyboard': [[{'text': 'Share my phone number', 'request_contact': True}]],
                                        'resize_keyboard': True,
                                        'one_time_keyboard': True,
                                    }
                                },
                                timeout=10,
                            )
                        except Exception as ex:
                            self.stderr.write(f"Error sending contact request keyboard: {ex}")

                    # Continue to next update after handling callback
                    continue

                # Handle standard messages
                msg = upd.get('message') or upd.get('edited_message') or {}
                if not msg:
                    continue

                chat = msg.get('chat') or {}
                from_user = msg.get('from') or {}
                chat_id = chat.get('id') or from_user.get('id')
                if not chat_id:
                    continue

                bot_user, created = BotUser.objects.get_or_create(
                    bot=bot,
                    telegram_id=chat_id,
                    defaults={
                        'username': from_user.get('username') or chat.get('username'),
                        'first_name': from_user.get('first_name') or chat.get('first_name'),
                        'last_name': from_user.get('last_name') or chat.get('last_name'),
                        'language_code': from_user.get('language_code') or chat.get('language_code'),
                        'last_seen_at': timezone.now(),
                    }
                )
                # Update missing/changed profile fields when provided
                update_fields = []
                for field, value in {
                    'username': from_user.get('username') or chat.get('username'),
                    'first_name': from_user.get('first_name') or chat.get('first_name'),
                    'last_name': from_user.get('last_name') or chat.get('last_name'),
                    'language_code': from_user.get('language_code') or chat.get('language_code'),
                }.items():
                    if value and getattr(bot_user, field) != value:
                        setattr(bot_user, field, value)
                        update_fields.append(field)
                bot_user.last_seen_at = timezone.now()
                update_fields.append('last_seen_at')
                if update_fields:
                    bot_user.save(update_fields=update_fields)

                text = (msg.get('text') or '').strip()

                # Save phone number if contact message
                contact = msg.get('contact') or {}
                if contact:
                    try:
                        self.stdout.write(self.style.WARNING("=== CONTACT RECEIVED (polling) ==="))
                        self.stdout.write(json.dumps(contact, indent=2))
                    except Exception:
                        self.stdout.write(str(contact))
                    phone = (contact.get('phone_number') or '').strip()
                    target_user_id = contact.get('user_id') or from_user.get('id') or chat_id
                    try:
                        bu, _ = BotUser.objects.get_or_create(
                            bot=bot,
                            telegram_id=target_user_id,
                            defaults={
                                'username': from_user.get('username') or chat.get('username'),
                                'first_name': from_user.get('first_name') or chat.get('first_name'),
                                'last_name': from_user.get('last_name') or chat.get('last_name'),
                                'language_code': from_user.get('language_code') or chat.get('language_code'),
                            }
                        )
                        if phone and (not bu.phone_number or bu.phone_number != phone):
                            bu.phone_number = phone
                            bu.save(update_fields=['phone_number'])
                            self.stdout.write(self.style.SUCCESS(f"✓ Saved phone for user {bu.telegram_id}: {phone}"))
                            # Hide the contact keyboard and unpin the request message(s)
                            try:
                                requests.post(
                                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                                    json={
                                        'chat_id': chat_id,
                                        'text': 'Thanks! Your phone number was received.',
                                        'reply_markup': { 'remove_keyboard': True },
                                    },
                                    timeout=10,
                                )
                            except Exception as ex:
                                self.stderr.write(f"Error sending confirmation/hiding keyboard: {ex}")
                            try:
                                # Unpin all to clean up the pinned prompt if present
                                requests.post(
                                    f"https://api.telegram.org/bot{bot_token}/unpinAllChatMessages",
                                    json={
                                        'chat_id': chat_id,
                                    },
                                    timeout=10,
                                )
                            except Exception as ex:
                                self.stderr.write(f"Error unpinning messages: {ex}")
                        else:
                            self.stdout.write(self.style.NOTICE(f"No phone saved. Existing={bu.phone_number!r} Incoming={phone!r}"))
                    except Exception as ex:
                        self.stderr.write(f"Error saving phone number: {ex}")

                # On /start: send pinned intro with button, set awaiting state, and request contact
                if text.startswith('/start'):
                    if not bot_user.started_at:
                        bot_user.started_at = timezone.now()
                    bot_user.state = 'await_button'
                    bot_user.save(update_fields=['started_at', 'state'] if bot_user.started_at else ['state'])

                    intro_text = (
                        "Welcome! Use the buttons below to ask a question or share your phone number."
                    )
                    try:
                        send_resp = requests.post(
                            f"https://api.telegram.org/bot{bot_token}/sendMessage",
                            json={
                                'chat_id': chat_id,
                                'text': intro_text,
                                'reply_markup': {
                                    'inline_keyboard': [
                                        [
                                            {
                                                'text': 'Ask a question',
                                                'callback_data': 'enable_questions'
                                            },
                                            {
                                                'text': 'Share my phone number',
                                                'callback_data': 'request_contact_btn'
                                            }
                                        ]
                                    ]
                                },
                            },
                            timeout=10,
                        )
                        send_js = send_resp.json()
                        message_to_pin_id = None
                        if send_js.get('ok') and send_js.get('result'):
                            message_to_pin_id = send_js['result'].get('message_id')
                        if message_to_pin_id:
                            try:
                                requests.post(
                                    f"https://api.telegram.org/bot{bot_token}/pinChatMessage",
                                    json={
                                        'chat_id': chat_id,
                                        'message_id': message_to_pin_id,
                                        'disable_notification': True,
                                    },
                                    timeout=10,
                                )
                            except Exception as ex:
                                self.stderr.write(f"Error pinning message: {ex}")
                    except Exception as ex:
                        self.stderr.write(f"Error sending intro/button: {ex}")

                    # a7aa7a 

                    # No separate contact request is sent here; use the inline button above

                else:
                    # Gate messages until enabled
                    if bot_user.state != 'enabled':
                        # Delete the incoming message to simulate blocking send
                        try:
                            incoming_message_id = msg.get('message_id')
                            if incoming_message_id is not None:
                                requests.post(
                                    f"https://api.telegram.org/bot{bot_token}/deleteMessage",
                                    json={
                                        'chat_id': chat_id,
                                        'message_id': incoming_message_id,
                                    },
                                    timeout=10,
                                )
                        except Exception as ex:
                            self.stderr.write(f"Error deleting gated message: {ex}")

                        try:
                            requests.post(
                                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                                json={
                                    'chat_id': chat_id,
                                    'text': 'Thanks! Press the button to ask another question.',
                                    'reply_markup': {
                                        'inline_keyboard': [[
                                            {
                                                'text': 'Ask another question',
                                                'callback_data': 'enable_questions'
                                            }
                                        ]]
                                    },
                                },
                                timeout=10,
                            )
                        except Exception as ex:
                            self.stderr.write(f"Error sending gate prompt: {ex}")
                        continue

                # Persist all messages
                MessageLog.objects.create(
                    bot=bot,
                    bot_user=bot_user,
                    message_id=str(msg.get('message_id')) if msg.get('message_id') is not None else None,
                    chat_id=chat_id,
                    from_user_id=from_user.get('id'),
                    text=text or None,
                    raw=msg,
                )

                # After accepting one question, close chat again until button is pressed
                if bot_user.state == 'enabled' and not text.startswith('/start'):
                    bot_user.state = 'await_button'
                    bot_user.save(update_fields=['state'])
                    try:
                        requests.post(
                            f"https://api.telegram.org/bot{bot_token}/sendMessage",
                            json={
                                'chat_id': chat_id,
                                'text': 'Thanks! Press the button to ask another question.',
                                'reply_markup': {
                                    'inline_keyboard': [[
                                        {
                                            'text': 'Ask another question',
                                            'callback_data': 'enable_questions'
                                        }
                                    ]]
                                },
                            },
                            timeout=10,
                        )
                    except Exception as ex:
                        self.stderr.write(f"Error sending relock prompt: {ex}")

            if not js.get('result'):
                time.sleep(sleep_sec)


