---
slug: 'type-embedded-in-name'
meta:
  last_update_date: 2022-04-19
  title: 'Type Embedded in Name'
  description: "playerName, dateString, userList: the type is already in the annotation, and now it's in the name too. Redundant today, misleading tomorrow when the type changes but the name doesn't."
  known_as:
    - Attribute Name and Attributes Type are Opposite
categories:
  expanse: 'Within'
  obstruction:
    - Couplers
  occurrence:
    - Names
  tags:
    - ---
  smell_hierarchies:
    - Code Smell
    - Implementation Smell
    - Linguistic Smell
relations:
  related_smells:
    - name: Primitive Obsession
      slug: primitive-obsession
      type:
        - co-exist
    - name: Uncommunicative Name
      slug: uncommunicative-name
      type:
        - co-exist
    - name: Duplicated Code
      slug: duplicated-code
      type:
        - causes
problems:
  general:
    - Duplication
    - Comprehensibility
  violation:
    principles:
      - Law of Demeter
    patterns:
      - ---
refactors:
  - Extract Class
  - Rename Method
  - Rename Variable
history:
  - author: 'William C. Wake'
    type: 'origin'
    named_as:
      - Type Embedded in Name
    regarded_as:
      - Code Smell (Names)
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

## Type Embedded in Name

Whenever a variable has an explicit type prefix or suffix, it can strongly signal that it should be just a class of its own. For example, `String currentDate = "2021-14-11"` embeds the potential class `Date` in the name of a variable, and that could also be classified as the [Primitive Obsession](./primitive-obsession.md) code smell.

Wake signals that the embedded type could also be in the method names, giving an example of a `schedule.addCourse(course)` method in contrast to `schedule.add(course)`. He notes that it could have been a matter of preference, although he insists that this can be a problem wherever some generalization occurs [[1](#sources)]. When a parent class for `Course` is introduced to cover not only _courses_ but also _series of courses_, then `addCourse()` has a name that is no longer appropriate, thus suggesting the usage of more neutral terms. [[1](#sources)] Parameters of a method are part of the method name, and this kind of naming is also a duplication.

When a variable has a declared type, repeating that type in its name is unnecessary. Names with embedded types for which no class yet exists are good candidates to be refactored into separate classes.

### Causation

This was a standard in pointer-based languages, but it is not helpful in modern Object-Oriented Programming languages. [[1](#sources)]

### Problems

#### **Duplication**

Both the argument and name mentions the same type.

#### **Comprehensibility Issues**

If the name of a variable is just precisely the name of the class, it's a case of [Uncommunicative Name](./uncommunicative-name.md).

### Examples

<div class="example-block">

#### Smelly

```java
String playerName = "Luzkan";
int playerHealth = 100;
int playerStamina = 50;
int playerAttack = 7;
```

#### Solution

```java
record Player(String name, int health, int stamina, int attack) {}

var luzkan = new Player("Luzkan", 100, 50, 7);
```

</div>

<div class="example-block">

#### Smelly

```java
Instant instant = Instant.now();
foo();
Instant instant2 = Instant.now();
```

#### Solution

```java
Instant fooBenchmarkStart = Instant.now();
foo();
Instant fooBenchmarkEnd = Instant.now();
```

</div>

### Refactoring:

- Extract Class
- Rename Method
- Rename Variable

---

##### Sources

- [[1](#sources)], [Origin] - William C. Wake, _"Refactoring Workbook"_ (2004)
