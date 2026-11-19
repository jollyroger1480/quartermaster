# Shop services (EXAMPLE — replace with your real services)

This file is what the bot tells callers and texters about your services.
Copy it to the folder `vault.root` points at (the example config uses
`~/callscoot/knowledge/services.md`).

The bot never gives a price, a discount, or a shipping cost. Money amounts in
this folder are stripped before the model sees them, and a price question gets
the hand-off line, so there is no point writing prices here.

# What to take down

- Junk removal: their name, town, and what they have. The owner calls back with the price and the time.
- E-waste: drop-off or pickup. The owner calls back with the price.
- Parts: the part, the year, make and model, and a part number if they have one. The owner gets back to them with a price.

# Services

- Scrap metal pickup — local area, by appointment
- Junk removal — by appointment
- E-waste / electronics recycling — drop-off or pickup
- Auto parts — shipping, or local pickup by appointment

Hours and the public address belong in `callscoot.toml` under `persona.shop_hours` and `persona.business_address`. Do not put a home address in this file.
