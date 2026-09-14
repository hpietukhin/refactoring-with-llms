---
slug: 'boolean-blindness'
meta:
  last_update_date: 2022-04-19
  title: 'Boolean Blindness'
  description: "Does filter(true) mean take or drop? When a function operates on raw booleans, it destroys the information about what those values represent. The type system knows; the reader doesn't."
  known_as:
    - ---
categories:
  expanse: 'Within'
  obstruction:
    - Lexical Abusers
  occurrence:
    - Names
  tags:
    - ---
  smell_hierarchies:
    - Code Smell
relations:
  related_smells:
    - name: Uncommunicative Name
      slug: uncommunicative-name
      type:
        - family
    - name: Magic Number
      slug: magic-number
      type:
        - family
    - name: '"What" Comments'
      slug: what-comment
      type:
        - causes
problems:
  general:
    - Comprehensibility
  violation:
    principles:
      - ---
    patterns:
      - ---
refactors:
  - Introduce New Type
history:
  - author: 'Marcel Jerzyk'
    type: 'origin'
    named_as:
      - Boolean Blindness
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

## Boolean Blindness

A selection API can leave an important question unanswered at the call site: does `true` mean `TAKE` or `DROP` (check [example](#example))? Boolean Blindness smell occurs when a method that operates on `boolean` values destroys the information about what each value represents. It would be much better to use an expressive type with appropriate names in these situations. For this selection method, an enum named `Keep` can define the values `DROP` and `TAKE`.

This smell is in the same family as [Uncommunicative Names](./uncommunicative-name.md) and [Magic Numbers](./magic-number.md).

### Problems:

#### Comprehensibility

Neither in real life one can answer just _yes_/_no_ without ever confusing interlocutor to every single closed question that may possible.

### Example

<div class="example-block">

#### Ambiguity of Boolean

```java
interface Decision<T> {
    boolean decide(T value);
}

interface Selector {
    <T> List<T> select(List<T> values, Decision<T> decision);
}

List<String> selected = selector.select(names, name -> !name.isBlank());
```

#### Meaningful Boolean

```java
enum Keep {
    DROP,
    TAKE
}

interface Decision<T> {
    Keep decide(T value);
}

interface Selector {
    <T> List<T> select(List<T> values, Decision<T> decision);
}

List<String> selected = selector.select(
    names,
    name -> name.isBlank() ? Keep.DROP : Keep.TAKE
);
```

</div>

### Refactoring:

- Introduce New Type

---

##### Sources

- [Origin] - Marcel Jerzyk, _"Code Smells: A Comprehensive Online Catalog and Taxonomy"_ (2022)
