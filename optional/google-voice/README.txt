SHOP ASSISTANT (Linux)
by Ewalt's Auto Tuning

An AI receptionist for a small shop. It answers calls to your Google Voice
number on this computer, answers questions from notes you write, takes
messages, and saves every call as a summary, transcript and recording.


WHAT YOU NEED
  - A 64-bit Linux desktop (Ubuntu, Mint, Fedora, Debian and similar) that
    stays on and logged in to its desktop. Intel/AMD, or 64-bit ARM.
  - Google Chrome, Microsoft Edge or Chromium installed.
  - A Google Voice number. Calls to that number are what it answers.
  - A free Groq account for the AI (the setup screen tells you where).
  - About 4 GB of free disk space and an internet connection.


INSTALL (once)
  1. Unpack the download somewhere in your home folder:
         tar -xzf ShopAssistant-*-linux.tar.gz
  2. Run the installer as your normal user (no sudo):
         cd ShopAssistant
         ./install.sh
  3. Wait. It downloads about 1 GB and takes 5 to 20 minutes.
  4. The control panel opens in your browser. "Shop Assistant" is also in
     your applications menu, or run ./shop-assistant.sh

Everything stays inside this folder. Nothing is installed system-wide.


SET UP (the Home screen lists these and ticks them off)
  1. Your business: name, your first name, hours, how you get back to people.
  2. Settings: paste your Groq key and press Save and test.
  3. What it knows: write your services, prices and common questions.
     The assistant only tells callers what you write here.
  4. Google Voice: press Sign in, sign in, open Voice, close that window.
     In Voice settings (gear, Calls) set calls to ring on Web and turn
     Screen calls off.
  5. Home: flip the switch to On, then call your Google Voice number from
     another phone. The first start takes a few minutes while it downloads
     the voice.

Use "Try it" any time to type questions and see what it would say.


DAY TO DAY
  - Calls: every call as a message slip. Click one for the transcript and
    the recording.
  - If you pick up on your own phone before its pickup delay (8 seconds by
    default, change it in Settings), it stays out of the call.
  - "Start by itself when this computer turns on" starts it each time you
    log in to the desktop. Turn on automatic login in your system settings
    if the computer should come back by itself after a restart.
  - The Google Voice window must stay open. You can minimise it.
  - Turn off suspend / sleep in your power settings, or calls go unanswered.


THINGS TO KNOW BEFORE YOU RELY ON IT
  - It is an assistant, not a person. It can mishear, and it only knows what
    you wrote down. Read the Calls page.
  - Calls are recorded on this computer. Some states require telling callers.
    If that applies to you, add "Calls may be recorded" to your greeting.
  - Google has no official way to automate Voice. This works by driving the
    Voice web page, so a Google redesign can stop it answering until the app
    is updated, and Google's terms restrict automated use of Voice. Use a
    number you could live without.
  - Your Groq key, settings, notes, call recordings, transcripts and the
    Google Voice sign-in are in the data folder here. Keep it private.
    Nothing is sent to Ewalt's Auto Tuning. What callers say and your notes
    are sent to the AI service you chose so it can answer.
  - It needs a desktop session (a screen you can log in to). It will not run
    on a server with no desktop.


WHERE THINGS ARE
  data/knowledge/        your notes (plain text, edit in the panel)
  data/logs/             calls, transcripts, recordings, activity log
  data/settings.json     your settings
  data/secrets.json      your AI key (keep this file private)
  data/browser-profile/  the Google Voice sign-in
  runtime/               the private Python and packages

COMMANDS (optional)
  ./shop-assistant.sh            open the control panel
  ./shop-assistant.sh doctor     check every part
  ./shop-assistant.sh bench      test this computer's speed
  ./shop-assistant.sh chat       type to the assistant in the terminal


REMOVE
  1. In the panel, turn the assistant Off and untick "Start by itself".
  2. Delete ~/.local/share/applications/shop-assistant.desktop
  3. Delete this folder.


See LICENSE.txt and CREDITS.md for the open-source projects this is built on.
