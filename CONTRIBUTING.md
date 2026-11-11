# Contributing

Thanks for helping with TraceGate. The project is small on purpose: one
Python script, one skill file, one installer, all standard library.

## Ground rules

1. **No dependencies.** Runtime code stays Python 3 standard library only. No
   `pip install`, no network calls, no API keys.
2. **No fake data.** Tests run the real scripts in temp directories with real
   git repos. Do not mock the parts you are testing. Do not invent prices in
   `pricing.json` - quote the provider's pricing page and date it.
3. **Never touch the user's git state.** Checkpoints use a temporary
   `GIT_INDEX_FILE` and dangling commits under `refs/tracegate/`. Nothing may
   commit on the user's branch, move HEAD, or touch the real staging area.
4. **Never log secrets.** Session logs are plain JSONL files.

## Set up and test

```bash
git clone https://github.com/A2wkiller/agent-rewind.git
cd agent-rewind
python3 -m unittest discover -s tests -v
```

Add a test for every behavior change. CI runs the same command on Python 3.9
and 3.12.

## Pull requests

- One change per PR, small commits, conventional style (`feat:`, `fix:`,
  `docs:`, `test:`, `security:`).
- Update the README "Honest status" section if you change what was verified.
- If you change installer target folders, say which real tool and version you
  verified them against.
