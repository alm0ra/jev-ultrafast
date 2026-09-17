# Jev Parallel 0.2.0

This is the consolidated local chat/browser release.

- DeepSeek V4.1 Flash interprets the request and prepares its search in one call.
- Jev selects browser operations and observed targets.
- Result explanations run in a background thread without holding the browser lock.
- Text entry waits for a generated value when needed.
- Chat shows results, errors and conversation context.
- Website discovery starts from search results instead of invented domains.
- macOS opens owned tabs through Chrome; the viewport uses the actual window size.
- Closed-tab cleanup and expired-session recovery are handled.
- Execution reports remain available in chat if the summary model fails.
- Background progress checks run every five actions and discard outdated advice.
- Autocomplete selection, custom clickable controls, loading shells and navigation cycles are handled.
- Runs have a 300-action limit; chats live in memory and are lost on restart.

Run `.venv/bin/jev` and open http://127.0.0.1:8766/.
Credentials stay in ignored `.env`; built distributions do not include them.

Validation: 64 offline Python tests, 3 JavaScript recovery scenarios, Ruff, JavaScript syntax checks and package build.
Full success on arbitrary websites, account creation and purchases is not guaranteed.
