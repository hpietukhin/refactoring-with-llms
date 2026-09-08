---
slug: 'null-check'
meta:
  last_update_date: 2022-04-19
  title: 'Null Check'
  description: 'Defensive null checks scattered everywhere like a nervous tic — each one a band-aid over a missing Null Object, and each a reminder that Tony Hoare called his invention a billion-dollar mistake.'
  known_as:
    - ---
categories:
  expanse: 'Between'
  obstruction:
    - Bloaters
  occurrence:
    - Conditional Logic
  tags:
    - Unknown
  smell_hierarchies:
    - Code Smell
    - Design Smell
relations:
  related_smells:
    - name: Special Case
      slug: special-case
      type:
        - family
    - name: Afraid To Fail
      slug: afraid-to-fail
      type:
        - caused
    - name: Flag Argument
      slug: flag-argument
      type:
        - causes
    - name: Conditional Complexity
      slug: conditional-complexity
      type:
        - causes
problems:
  general:
    - Duplication
    - Increased Complexity
    - Bijection
  violation:
    principles:
      - ---
    patterns:
      - ---
refactors:
  - Introduce Null Object
  - Introduce Maybe
  - Introduce Optional
history:
  - author: 'William C. Wake'
    type: 'origin'
    named_as:
      - Null Check
    regarded_as:
      - Code Smell
    source:
      year: 2004
      authors:
        - William C. Wake
      name: 'Refactoring Workbook'
      type: 'book'
      href:
        isbn_13: '978-0321109293'
        isbn_10: '0321109295'
---

## Null Check

Null check is widespread everywhere because the programming languages allow it. It causes a multitude of `undefined` or `null` checks everywhere: in guard checks, in condition blocks, and verifications clauses. Instead, special objects could be created that implement the missing-event behavior, errors could be thrown and catched, and many duplications would be removed. Even an anecdote sometimes appears here and there on discussion forums that the inventor of the `null` reference, Tony Hoare (also known as the creator of the QuickSort algorithm), apologizes for its invention and calls it a _billion-dollar mistake_.

_Null Check_ is a special case of [Special Case](./special-case.md) code smell.

### Causation

The direct cause of null checking is the lack of a proper Null Object that might implement the object's behavior in case it's null. There is a strong opinion that `null` or `undefined` is a detrimentally bad idea in programming languages [[1](#sources)].

### Problems

#### **Duplication**

Usually, the null check reoccurs.

#### **Increased Complexity**

Special cases must be made for an object that might be undefined.

#### **Bijection Violation**

A `null`/`undefined` as a model is not in a one-to-one relationship with the domain. Moreover, there is no representation.

### Examples

<div class="example-block">

#### Smelly

```java
interface BonusDamage {
    double increaseDamage(double damage);
}

final class Critical implements BonusDamage {
    private final double multiplier;

    Critical(double multiplier) {
        this.multiplier = multiplier;
    }

    public double increaseDamage(double damage) {
        return damage + damage * multiplier * Math.random() * 2;
    }
}

final class Magical implements BonusDamage {
    private final double multiplier;

    Magical(double multiplier) {
        this.multiplier = multiplier;
    }

    public double increaseDamage(double damage) {
        return damage * multiplier;
    }
}

void applyBonusDamage(Perk perk) {
    BonusDamage bonusDamage = perk.getBonusDamage();
    if (bonusDamage == null) {
        return;
    }
    // ...
}
```

#### Solution

```java
interface BonusDamage {
    double increaseDamage(double damage);
}

final class Critical implements BonusDamage {
    private final double multiplier;

    Critical(double multiplier) {
        this.multiplier = multiplier;
    }

    public double increaseDamage(double damage) {
        return damage + damage * multiplier * Math.random() * 2;
    }
}

final class Magical implements BonusDamage {
    private final double multiplier;

    Magical(double multiplier) {
        this.multiplier = multiplier;
    }

    public double increaseDamage(double damage) {
        return damage * multiplier;
    }
}

final class NullBonusDamage implements BonusDamage {
    public double increaseDamage(double damage) {
        return damage;
    }
}

final class Perk {
    private final BonusDamage bonusDamage;

    Perk(BonusDamage bonusDamage) {
        this.bonusDamage = bonusDamage == null
                ? new NullBonusDamage()
                : bonusDamage;
    }

    BonusDamage getBonusDamage() {
        return bonusDamage;
    }
}

double applyBonusDamage(Perk perk, double damage) {
    return perk.getBonusDamage().increaseDamage(damage);
}
```

</div>

### Refactoring:

- Introduce Null Object
- Introduce `Maybe`/`Optional`

### Exceptions

Similar to the `if` statements, one usually is not problematic. Creating a separate Null Object to handle this case (for example, when it is only in the Factory method [WAKE]) might not be worth the hassle.

---

##### Sources

- [[1](#sources)], [Origin] - William C. Wake, _"Refactoring Workbook"_ (2004)
