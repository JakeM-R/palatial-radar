# Physical AI Radar (for Palatial)

What moved in robot simulation and physical AI this week: Federal Register notices, releases of the simulators Palatial exports to, funding, launches and research. Live at https://jakem-r.github.io/palatial-radar/

- `fetch.py` pulls the feeds, sorts items with keyword rules, and writes `site/data.json`. Standard library only.
- `.github/workflows/refresh.yml` runs it daily at 06:00 UTC and deploys `site/` to GitHub Pages.
- In production the keyword rules would be replaced by an LLM pass that summarises and scores each item.

Built by Jake Makking-Robinson. Not affiliated with Palatial.
