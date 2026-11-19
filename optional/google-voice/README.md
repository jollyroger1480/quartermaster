# Optional: Google Voice

This folder is **not** the Bluetooth receptionist. `callscoot watch` still
answers a phone paired over Bluetooth HFP and wireless ADB. Nothing in here
starts with that watcher.

Use this when you want a Google Voice number instead of a handset on the
bench. It is [Shop Assistant](https://www.ewaltsautotuning.com/) by
[Ewalt's Auto Tuning](https://www.ewaltsautotuning.com/) (Evan Ewalt,
Watertown, SD). The Linux copy is included under his MIT license. See
`LICENSE.txt`. His own walkthrough is `README.txt`.

Shop Assistant opens Google Voice in Chrome, Edge, or Chromium and answers
calls to that number. It is a separate app: its own panel, its own notes in
`data/knowledge/`, and its own Groq key in `data/secrets.json`. It does not
read `callscoot.toml`.

## Set up

From this folder, as your normal user:

```bash
./install.sh
```

That downloads a private Python and the speech packages into `runtime/`
(about 1 GB). Then the panel opens. Sign in to Google Voice, set calls to
ring on the web, and turn Screen calls off. Flip the switch to On and call
the Voice number from another phone.

Next time: `./shop-assistant.sh`

`runtime/` and `data/` stay on your machine. They are gitignored.

## Before you rely on it

Google has no official way to automate Voice. This drives the Voice web
page, so a Google redesign can stop it answering until the app is updated.
Use a number you can live without. The Voice window has to stay open. The
computer has to stay logged in to a desktop.

The Bluetooth path in the rest of this repo does not have those limits. If
you have a phone, use that.
