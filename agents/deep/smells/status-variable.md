---
slug: 'status-variable'
meta:
  last_update_date: 2022-04-19
  title: 'Status Variable'
  description: 'found = False. Then a loop. Then found = True somewhere inside. Then a check after. Mutable flags that complicate control flow when a direct return or a built-in would express the same logic in a single line.'
  known_as:
    - ---
categories:
  expanse: 'Within'
  obstruction:
    - Obfuscators
  occurrence:
    - Unnecessary Complexity
  tags:
    - ---
  smell_hierarchies:
    - Code Smell
relations:
  related_smells:
    - name: Special Case
      slug: special-case
      type:
        - family
    - name: Clever Code
      slug: clever-code
      type:
        - co-exist
    - name: Afraid to Fail
      slug: afraid-to-fail
      type:
        - co-exist
    - name: Mutable Data
      slug: mutable-data
      type:
        - co-exist
    - name: Binary Operator in Name
      slug: binary-operator-in-name
      type:
        - co-exist
    - name: Loops
      slug: imperative-loops
      type:
        - caused
problems:
  general:
    - Comprehensibility
  violation:
    principles:
      - ---
    patterns:
      - ---
refactors:
  - Replace with Built-In
  - Extract Method
  - Remove Status Variables
history:
  - author: 'Marcel Jerzyk'
    type: 'origin'
    named_as:
      - Status Variable
    regarded_as:
      - Code Smell
    source:
      year: 2023
      authors:
        - Marcel Jerzyk
      name: 'Code Smells: A Comprehensive Online Catalog and Taxonomy'
      type: 'paper'
      href:
        direct_url: 'https://doi.org/10.1007/978-3-031-25695-0_24'
---

## Status Variable

Status Variables are mutable primitives that are initialized before an operation to store some information based on the process and are later used as a switch for some action.

The _Status Variables_ can be identified as a distinct code smell, although they are just a signal for five other code smells:

- [Clever Code](./clever-code.md),
- [Imperative Loops](./imperative-loops.md),
- [Afraid To Fail](./afraid-to-fail.md),
- [Mutable Data](./mutable-data.md),
- [Special Case](./special-case.md).

They come in different types and forms, but common examples are `boolean success = false` before an operation or `int i = 0` before a loop. The code that has them increases in complexity by a lot, and usually for no particular reason because there most likely exists a proper solution using first-class functions. Sometimes, they clutter the code, demanding other methods or classes to make [additional checks](./special-case.md) before execution resulting in [Required Setup/Teardown Code](./required-setup-or-teardown-code.md).

### Causation

The developer might have special cases that could be handled only inside a loop and could not figure out a better solution.

### Problems

#### **Comprehensibility**

It is more difficult to understand the inner workings of a method than the declarative solution.

### Examples

<div class="example-block">

#### Smelly

```java
int findFooIndex(List<String> names) {
    boolean found = false;
    int index = 0;
    while (!found && index < names.size()) {
        if (names.get(index).equals("foo")) {
            found = true;
        } else {
            index++;
        }
    }
    return found ? index : -1;
}
```

#### Solution

Solution, which removes the usage of Status Variables.

```java
int findFooIndex(List<String> names) {
    for (int index = 0; index < names.size(); index++) {
        if (names.get(index).equals("foo")) {
            return index;
        }
    }
    return -1;
}
```

#### Solution

Solution, which removes the usage of Status Variables and [Clever Code](./clever-code.md).

```java
int findFooIndex(List<String> names) {
    return names.indexOf("foo");
}
```

</div>

### Refactoring:

- Replace with Built-In
- Extract Method
- Remove Status Variables

---

##### Sources

- [Origin] - Marcel Jerzyk, _"Code Smells: A Comprehensive Online Catalog and Taxonomy"_ (2022)
