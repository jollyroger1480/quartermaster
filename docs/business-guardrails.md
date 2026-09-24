# Business guardrails

The bot answers from two places: a few lines in `callscoot.toml`, and the `.md` files in the folder `vault.root` points at. The chat request sends the shop notes and the caller's words. It does not send a tool list, a web search, or a weather plugin. A price the notes do not contain is a price the bot is told not to say.

Write the number down, or write that there is no set price. A blank line is how a phone bot invents one.

## 1. Point the brain at your model

The first `[[llm.providers]]` block is the brain. Later blocks run only if that one fails.

```toml
[[llm.providers]]
name = "glm"
base_url = "https://api.z.ai/api/coding/paas/v4"
model = "glm-5.3-flash"
api_key_env = "ZAI_API_KEY"
```

`model` is the model name. `api_key_env` is the **name** of a variable in `.env`, never the key itself. `.env` stays off git.

```bash
# .env  (this file is gitignored)
ZAI_API_KEY=your-key-here
```

One provider is enough. Skip "agent" or "compound" models from a vendor quickstart. Those starters often ship a weather tool and a general-assistant prompt, then answer the weather when a customer asked for a price.

## 2. Hours and the public address

These two lines are the only hours and address the prompt allows the bot to speak. Leave one empty and the prompt says it was not provided.

```toml
[persona]
shop = "Black Pearl Salvage"
owner = "Anne"
name = "the shop assistant"
shop_hours = "By appointment, Tuesday through Saturday"
business_address = "Yourtown, ST 00000"
system_extra = ""
```

Put the shop line a customer is allowed to hear. A home address does not belong here. The call prompt also refuses personal phone, schedule, family, and legal matters.

## 3. The price list

`vault.root` in the example config is `~/callscoot/knowledge`. Copy the template into that folder and replace the sample lines with yours:

```bash
mkdir -p ~/callscoot/knowledge
cp knowledge/services.example.md ~/callscoot/knowledge/services.md
```

A price file the bot can quote looks like this:

```markdown
# Prices

Say these numbers exactly. If a line says "no set price", do not invent one.

- Used brake rotor, most cars: $40 plus shipping. Confirm year, make, and model before saying it fits.
- Scrap metal pickup: free inside Yourtown. No charge.
- Junk removal: no set price. Take their name, town, and what they have. Anne calls back with the number and the time.
- Labor: no set price. Anne quotes it.
```

Same rule for anything else you want said out loud: shipping, warranty, "we do not take cards on the phone", which towns you cover. One fact per line. If it is not in this folder, the prompt says to tell the caller the owner will check and follow up.

Drop extra notes in as more `.md` or `.txt` files in that same folder. Policies, fitment notes, a short FAQ. The bot searches them on each turn. It does not browse the web.

## 4. Shop rules, in your own words

`persona.system_extra` is appended to the phone prompt. One rule per line.

```toml
system_extra = """
Never quote a dollar amount that is not written in the shop notes.
Never say a part fits a vehicle unless the notes name that vehicle.
Do not take card numbers on the phone. Tell them Anne will send a link.
"""
```

Texts use the same notes and the same "do not invent a price" rule. They do not read `system_extra`. Put text-facing rules in the markdown too.

## 5. Lines the model does not get to rewrite

These are spoken as written:

| Key | When |
|---|---|
| `call.greeting` | right after answer |
| `call.farewell` | caller says goodbye, in a few words |
| `call.voicemail_reply` | the second empty listen. The first empty listen says "Are you still there?" That line is in the code, not in the config. |
| `call.arrange_reply` | someone asks to come by the house, meet in person, or get a home address, and it is not a scrap or junk job |
| `call.spam_reply` | the caller sounds like a warranty, IRS, or listing robocall |

`call.arrange_reply` and `call.spam_reply` skip the model. Scrap, junk, haul-away, and e-waste still go to the model, which may describe the service and may quote only what the notes show. It still may not lock a date or a time. It takes the name, the town, and what they have, and says the owner will call back.

`spam.hangup_on_scam = true` hangs up after the spam line. `spam.blacklist` is numbers that are never answered.

## 6. Let the customer finish

```toml
record_silence_s = 1.5
record_max_s = 14
```

The bot waits until its own greeting is done, then listens. `record_silence_s` is how long a pause lasts before it takes a turn. `1.0` already cut people off mid-sentence. `1.5` is the setting that stopped doing that. A turn still ends around 16 seconds so a monologue cannot hold the line open.

The bot does not listen while it is talking, so it cannot jump in halfway through a sentence.

## 7. Hear it before a customer does

`callscoot ask` runs the same prompt and the same notes, with no phone:

```bash
bin/callscoot ask "how much is a used brake rotor"
bin/callscoot ask "how much to haul a couch"
bin/callscoot ask "what's the weather"
bin/callscoot ask "what's your home address"
```

A rotor with `$40` in the notes should come back as that number. A couch with "no set price" should come back as a callback, not a made-up dollar amount. Weather and a home address should come back as a message for the owner.

`ask` does not fire the fixed spam and arrange lines. Those run on a live call only. Read them in `callscoot.toml` and you know exactly what a customer would hear.
