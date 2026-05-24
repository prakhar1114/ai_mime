# Fallback Plan for Weather Report Skill

This fallback plan details the manual steps to retrieve the weather report if the automated API call fails.

## Subtask 1 — Retrieve Weather Information
Intent: Find the current temperature and conditions for the requested location.

### Manual Steps:
1. Open the browser and navigate to Google: `https://www.google.com`.
2. Search for: `weather in <location>`.
3. Locate the temperature display element and the current condition text (e.g. "Sunny", "Cloudy", "Rain").
4. Copy the temperature value and condition description.

### UI Agent / Selectors Guidance:
- Google Weather Widget selector for temperature: `span#wob_tm` or class `.wob_t`.
- Weather condition selector: `span#wob_dc`.
- Input selector on Google Search: `textarea[name="q"]`.
