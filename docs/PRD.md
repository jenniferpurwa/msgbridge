# PRD: Personal Messaging Context Connector

**Author:** Jennifer Purwa · **Written:** Oct 17, 2026 · **Companion:** [Tech design](tech-design.md)

**Priority:** iMessage first, WhatsApp second
**Target user:** a single user (personal tool)
**Platform:** macOS, ChatGPT and Claude

> **Status (Nov 2026):** built. iMessage/SMS (M1), WhatsApp for Mac (M3) and ChatGPT over
> OAuth (M2) all work, and Claude Desktop connects locally. ChatGPT custom connectors were
> confirmed to work on the Plus plan, on both the web and the mobile app. Still open: the WhatsApp iPhone-backup
> backfill for older history, and the hardening items in M4.

## TL;DR

A small, read-only service on my Mac that indexes my iMessage and WhatsApp history and
exposes it to ChatGPT (and Claude) as a connector. I can say "help me reply to Stephen" and
the assistant finds the right thread itself.

## Problem

Giving ChatGPT context on a conversation today means copying and pasting from Messages or
WhatsApp, which breaks down:

- Copy-paste is limited to a certain number of lines, so one thread takes several rounds.
- Photos, links and previews interrupt the selection, so pastes come out broken.
- Context goes stale: every new message means pasting again.
- ChatGPT only sees the slice I grabbed, never the whole relationship.

## Goals

- Full history of iMessage (including SMS) and WhatsApp, synced in near real time: new
  messages can be queried within about a minute while the Mac is awake.
- Look up threads by name, nickname or group name, not by phone number.
- Clean text: photos, links, voice notes and reactions show as readable placeholders
  instead of breaking the thread.
- Works from ChatGPT (web, ideally mobile) through a connector. *(Both confirmed.)*

## Non-goals (v1)

- **Sending messages.** The assistant drafts; I copy and send. A send tool is a v2 decision
  and would need a hard confirmation step.
- Android, Telegram, Signal, email, Slack.
- A multi-user product. It's just for me.
- Writing to the Messages or WhatsApp databases, ever.

## Experience

After a one-time setup, the assistant reads my message history on demand and keeps up with
new messages on its own:

1. I open ChatGPT as usual.
2. I ask a question that mentions a person, conversation or message.
3. ChatGPT retrieves the right messaging history by itself.
4. It uses that history to answer.

No copying, pasting, uploading or exporting after setup.

## User stories

1. "For my chat with Stephen, help me draft the next response." → finds Stephen's thread,
   reads recent context (and older history if needed), drafts in my voice.
2. "How would you interpret the last message from Maya?" → finds Maya's latest message and
   reads the context around it.
3. "What did Alex and I decide about dinner plans last month?" → keyword and date search
   across threads.
4. "Who haven't I replied to?" → lists threads where the last message is from them and
   unanswered.
5. "Summarize the 'Bali trip' group chat since Monday."

## Functional requirements

- Each message has a timestamp, sender ("Me" or a name), app, thread, text, what it replies
  to, and an attachment placeholder.
- Placeholders: `[photo]`, `[link: domain.com – title]`, `[voice note 0:42]`, `[sticker]`,
  `[reacted ❤️ to "…"]`.
- Phone numbers and emails are mapped to names through macOS Contacts.
- Fuzzy matching ("Steph" → Stephen) and disambiguation: if there are two Stephens, the tool
  returns both and the assistant asks me which one.
- Every tool is read-only.
- Default to the last ~200 messages; the assistant pages back as needed.
- Handle edits and unsends: show the latest text and mark it "edited".

## Group threads

- **By group name:** "Summarize the Bali trip chat" — conversation search covers group names too.
- **By member:** "my group chat with Stephen and Maya" returns groups containing both people,
  ranked by recent activity.
- Unnamed groups (common in iMessage) get a label generated from their members, e.g.
  "Stephen, Maya +2".
- When I ask about a person, their 1:1 thread comes first, then the groups they're in, so
  the assistant doesn't mix them up.
- Every message is labeled with the sender's name, not just "them", so the assistant can
  tell who said what.

## Clarity on sources

I want the assistant to tell me which messages it actually read, especially when it
interprets someone's behavior. For example:

> Based on Stephen's last 20 messages (Oct 1–17), here is my interpretation…

This separates analysis grounded in the messages it retrieved from guesses based on partial
history or memory.

## Constraints

- Must work on the ChatGPT Plus plan ($20/month).

## Privacy and security

The questions I started with:

- What gets exposed online, and what stays on the Mac?
- Do I need authentication?
- Is there anything sensitive to redact?

The short answers: yes, it needs auth; not everything stays local, because any message the
assistant reads is sent to that AI provider; and yes, one-time codes and card and ID
numbers should be redacted by default. See
[Privacy, security and auth](tech-design.md#privacy-security-and-auth) in the tech design.
