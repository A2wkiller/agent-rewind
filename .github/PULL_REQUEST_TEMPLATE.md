## Description
<!-- Provide a clear summary of what this PR does, why it is needed, and link any related issues. -->
Fixes #(issue)
## Type of Change
- [ ] 🐛 Bug fix (non-breaking change fixing an issue)
- [ ] ✨ New feature (non-breaking change adding functionality)
- [ ] 💥 Breaking change (fix or feature causing existing behavior to change)
- [ ] 📝 Documentation update
- [ ] 🧪 Tests / CI optimization
## Architectural Checklist
- [ ] **DAG Invariant Maintained**: No retroactive mutation of historical nodes in `tracegate_dag.sqlite3`.
- [ ] **Multi-Provider Consistency**: Verified compatibility across supported provider formats (Anthropic, Gemini, O
- [ ] **Token Calculation**: Token counts verified via native tokenizers or standard test fixtures.
- [ ] **Tests Added**: New functionality is covered in `test_tracegate_agentic_loop.py`.
- [ ] **All Tests Pass**: Ran `pytest test_tracegate_agentic_loop.py -v` locally with zero failures.
## Verification & Screenshots
<!-- Paste your test output (`pytest`) or provide a screenshot if making UI changes. -->
