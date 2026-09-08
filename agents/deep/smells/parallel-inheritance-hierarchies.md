---
slug: 'parallel-inheritance-hierarchies'
meta:
  last_update_date: 2022-04-19
  title: 'Parallel Inheritance Hierarchies'
  description: 'Add a BasicUser, and you need a BasicFunctions. Add a PremiumUser, and here comes PremiumFunctions. Every subclass in one hierarchy demands a mirror in the other, and the cost of every new feature doubles.'
  known_as:
    - ---
categories:
  expanse: 'Between'
  obstruction:
    - Change Preventers
  occurrence:
    - Responsibility
  tags:
    - ---
  smell_hierarchies:
    - Code Smell
relations:
  related_smells:
    - name: Shotgun Surgery
      slug: shotgun-surgery
      type:
        - family
    - name: Combinatorial Explosion
      slug: combinatorial-explosion
      type:
        - family
    - name: Duplicated Code
      slug: duplicated-code
      type:
        - causes
problems:
  general:
    - Duplication
  violation:
    principles:
      - ---
    patterns:
      - ---
refactors:
  - Move Method
  - Move Field
  - Create Partial
  - Fold Hierarchy into One
history:
  - author: 'Martin Fowler'
    type: 'origin'
    named_as:
      - Parallel Inheritance Hierarchies
    regarded_as:
      - Code Smell
    source:
      year: 1999
      authors:
        - Martin Fowler
        - Kent Beck (contributor)
        - Don Roberts (contributor)
      name: 'Refactoring: Improving the Design of Existing Code'
      type: 'book'
      href:
        isbn_13: '978-0201485677'
        isbn_10: '0201485672'
---

## Parallel Inheritance Hierarchies

This occurs when an inheritance tree depends on another inheritance tree by composition, and to create a subclass for a class, one finds that he has to make a subclass for another class. Fowler specified that this is a special case of [Shotgun Surgery](./shotgun-surgery.md) code smell.

### Causation

This smell can happen naturally when trying to model a problem in a domain. The problem arises when these hierarchies are created artificially and unnecessarily (for example, by adding a standard prefix throughout the classes).

### Problems

#### **Duplication**

Requires additional work to be done, which might be redundant.

### Examples

<div class="example-block">

#### Smelly

```java
abstract class User {
    Functions functions;
}

abstract class Functions {}

class BasicUser extends User {}

class BasicFunctions extends Functions {}

class PremiumUser extends User {}

class PremiumFunctions extends Functions {}

// Each new user requires a function subclass with the same prefix.
```

#### Solution

When solving, one must be cautious to not violate the _Single Responsibility Principle_.

```java
enum Feature {
    READ,
    EXPORT_REPORTS
}

abstract class User {
    abstract Set<Feature> availableFeatures();
}

class BasicUser extends User {
    @Override
    Set<Feature> availableFeatures() {
        return Set.of(Feature.READ);
    }
}

class PremiumUser extends User {
    @Override
    Set<Feature> availableFeatures() {
        return Set.of(Feature.READ, Feature.EXPORT_REPORTS);
    }
}

// Adding a user type no longer requires a matching Functions subclass.
```

</div>

### Refactoring:

- Move Method
- Move Field
- Create Partial
- Fold Hierarchy into One

---

##### Sources

- [Origin] - Martin Fowler, _"Refactoring: Improving the Design of Existing Code"_ (1999)
