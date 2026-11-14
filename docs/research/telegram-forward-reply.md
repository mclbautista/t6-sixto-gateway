# Telegram forwards and replies

Checked 2026-11-14 against the [Telegram Bot API `forwardMessage` documentation](https://core.telegram.org/bots/api#forwardmessage) (Bot API 10.4). Its parameter names are:

- Required: `chat_id`, `from_chat_id`, `message_id`.
- Optional: `message_thread_id`, `direct_messages_topic_id`, `video_start_timestamp`, `disable_notification`, `protect_content`, `message_effect_id`, `suggested_post_parameters`.

There is no reply parameter or option to link the forward as a reply to a specific status message. `message_thread_id` selects a forum topic (in supported supergroups or bot private chats with forum topic mode); `direct_messages_topic_id` selects a direct messages topic. These route the forward to a topic, rather than make it a reply to the status message. By contrast, [`copyMessage`](https://core.telegram.org/bots/api#copymessage) lists `reply_parameters`, but copying is out of scope.

The decision stands: `/forward` should forward the freelancer's file without a reply, then edit the waiting person's status message to say **“Delivered, see below”**. Do not use `copyMessage`.
