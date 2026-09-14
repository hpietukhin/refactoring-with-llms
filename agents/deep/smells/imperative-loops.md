---
slug: 'imperative-loops'
meta:
  last_update_date: 2022-04-19
  title: 'Imperative Loops'
  description: 'for (int i = 0; i < size; i++) — the ceremony of manually tracking indexes, accumulating results, and handling off-by-one errors, when a map, filter, or built-in says the same thing in one line.'
  known_as:
    - Explicitly Indexed Loops
    - Indexed Loops
    - Loops
categories:
  expanse: 'Within'
  obstruction:
    - Functional Abusers
  occurrence:
    - Unnecessary Complexity
  tags:
    - ---
  smell_hierarchies:
    - Code Smell
relations:
  related_smells:
    - name: Conditional Complexity
      slug: conditional-complexity
      type:
        - causes
    - name: Temporary Field
      slug: temporary-field
      type:
        - causes
    - name: Flag Arguments
      slug: flag-argument
      type:
        - causes
    - name: Obscured Intent
      slug: obscured-intent
      type:
        - causes
    - name: Status Variable
      slug: status-variable
      type:
        - causes
problems:
  general:
    - Readability
    - Increased Complexity
  violation:
    principles:
      - ---
    patterns:
      - ---
refactors:
  - Replace Loop with Pipeline
  - Replace Loop with built-in
history:
  - author: 'Marcel Jerzyk'
    type: 'origin'
    named_as:
      - Imperative Loops
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
  - author: 'Martin Fowler'
    type: 'parentage'
    named_as:
      - Loops
    regarded_as:
      - Code Smell
    source:
      year: 2018
      authors:
        - Martin Fowler
      name: 'Refactoring: Improving the Design of Existing Code'
      named_as: 'Loops'
      regarded_as: 'Code Smell'
      type: 'book'
      href:
        isbn_13: '978-0201485677'
        isbn_10: '0201485672'
---

## Imperative Loops

Martin Fowler has the feeling that loops are an outdated concept. He already mentioned them as an issue in his first edition of "Refactoring: Improving the design of existing code" book, although there were no better alternatives at that time. [[1](#sources)] Nowadays, languages provide an alternative - pipelines. Fowler, in his 2018 book (third edition), suggests that anachronistic loops should be replaced by pipeline operations such as `filter`, `map`, or `reduce` [[2](#sources)].

Indeed, loops can sometimes be hard to read and error-prone. This might be unconfirmed, but I doubt the existence of a programmer who has never had an `IndexOutOfBoundsException` at least once before. The recommended approach would be to avoid explicitly indexed loops and use Java's enhanced `for` loop, `forEach`, or a `Stream` pipeline. These alternatives take care of the indexing. Still, one should consider whether he is not about to write [Clever Code](./clever-code.md) and check if there is already a built-in method that will take care of the desired operation.

I would abstain from specifying all the loops as code smells. Loops have always been and probably are still going to be a fundamental part of programming. Java offers concise alternatives such as enhanced `for` loops and `Stream` pipelines. It is the indexation part might be the main problem. Of course, so are long loops or loops with side effects, but these are just a part of [Long Method](./long-method.md) or [side effects](./side-effects.md) code smells.

However, it is worth taking what is good from functional languages (such as `streams` or the immutability of the data) and implement those as widely as possible and conveniently, to increase the reliability of the application.

### Causation

It is impossible to easily overwrite the information given in the old books or video tutorials, which was pretty standard due to the lack of any other alternatives. People have learned that type of looping and may not even suspect that there are alternatives until they find them. Similarly, developers who come from older languages, which do not yet offer such facilities, can use explicit iteration habitually.

### Problems

#### **Readability**

In contrast to pipelines, loops don't provide declarative readability of what is precisely being processed.

### Example

<div class="example-block">

#### Smelly

Explicitly Indexed Loop

```java
List<String> examples = List.of("foo", "bar", "baz");
for (int index = 0; index < examples.size(); index++) {
    System.out.println(examples.get(index));
}
```

#### Solution

Readable `forEach` loop

```java
List<String> examples = List.of("foo", "bar", "baz");
examples.forEach(System.out::println);
```

</div>

<div class="example-block">

#### Smelly

[Clever Code](./clever-code.md), [Flag](./flag-argument.md) and Explicit Iterator Loop

```java
List<String> examples = List.of("foo", "bar", "baz");
boolean barInExamples = false;
for (int index = 0; index < examples.size(); index++) {
    if (examples.get(index).equals("bar")) {
        barInExamples = true;
    }
}
System.out.println(barInExamples); // true
```

#### Solution

Built-in method instead

```java
List<String> examples = List.of("foo", "bar", "baz");
System.out.println(examples.contains("bar")); // true
```

</div>

### Refactoring:

- Replace Loop with Pipeline
- Replace Loop with built-in

---

##### Sources

- [[1](#sources)], [Parentage] - Martin Fowler, "Refactoring: Improving the Design of Existing Code" (1999)
- [[2](#sources)] - Martin Fowler, "Refactoring: Improving the Design of Existing Code (3rd Edition)" (2018)
- [Origin] - Marcel Jerzyk, _"Code Smells: A Comprehensive Online Catalog and Taxonomy"_ (2022)
