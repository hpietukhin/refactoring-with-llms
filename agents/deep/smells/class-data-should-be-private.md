# Class Data Should Be Private

Make mutable representation private and expose the smallest behavior-oriented operation needed by callers. Avoid public getters and setters that merely preserve direct data access.

Use the smallest semantic change that removes the smell and rerun the relevant tests.
