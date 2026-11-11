## Description
<!-- What does this PR change and why? Link related issues. -->
Fixes #(issue)

## Checklist
- [ ] Runtime code is still Python 3 standard library only, no network, no API keys
- [ ] New behavior is covered by a real test in `tests/` (runs the actual script, no mocks of the thing being tested)
- [ ] `python3 -m unittest discover -s tests -v` passes locally
- [ ] User git state is untouched: no commits on the user's branch, real index and staging area never modified
- [ ] README "Honest status" updated if what was verified has changed
