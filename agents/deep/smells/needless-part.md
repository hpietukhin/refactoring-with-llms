# Needless Part

Delete a redundant abstraction only after confirming callers and tests do not need it. Inline or move behavior to the natural owner rather than adding a replacement wrapper.

Use the smallest semantic change that removes the smell and rerun the relevant tests.
