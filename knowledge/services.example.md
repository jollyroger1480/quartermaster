# Shop services (EXAMPLE — replace with your real services)

This file is what the bot quotes to callers and texters. Copy it to the
folder `vault.root` points at (the example config uses `~/callscoot/knowledge/services.md`).
Say a number only when it is written below. A line that says "no set price"
means take a message. Never invent a price, a date, or a time.

# Prices

- Used brake rotor, most cars: $40 plus shipping. Confirm year, make, and model before saying it fits.
- Scrap metal pickup: free inside Yourtown. No charge.
- Junk removal: no set price. Take their name, town, and what they have. The owner calls back with the number and the time.
- E-waste drop-off: no set price. Cheaper than pickup. Owner quotes it.
- Labor: no set price. Owner quotes it.

# Services

- Scrap metal pickup — local area, by appointment
- Junk removal — by appointment
- E-waste / electronics recycling — drop-off or pickup
- Auto parts — shipping, or local pickup by appointment

Hours and the public address belong in `callscoot.toml` under `persona.shop_hours` and `persona.business_address`. Do not put a home address in this file.
