---
slug: 'what-comment'
meta:
  last_update_date: 2022-04-19
  title: '"What" Comment'
  description: 'Comments that narrate what the code does instead of why — a deodorant sprayed over smelly code, where extracting a well-named method would eliminate both the smell and the comment.'
  known_as:
    - Comment
categories:
  expanse: 'Within'
  obstruction:
    - Dispensables
  occurrence:
    - Names
  tags:
    - ---
  smell_hierarchies:
    - Code Smell
relations:
  related_smells:
    - name: Fallacious Comment
      slug: fallacious-comment
      type:
        - family
    - name: Uncommunicative Name
      slug: uncommunicative-name
      type:
        - caused
    - name: Magic Number
      slug: magic-number
      type:
        - caused
    - name: Boolean Blindness
      slug: boolean-blindness
      type:
        - caused
    - name: Complicated Regex Expression
      slug: complicated-regex-expression
      type:
        - caused
    - name: Complicated Boolean Expression
      slug: complicated-boolean-expression
      type:
        - caused
    - name: Obscured Intent
      slug: obscured-intent
      type:
        - caused
problems:
  general:
    - Duplication
    - Cover Other Smells
  violation:
    principles:
      - ---
    patterns:
      - ---
refactors:
  - Extract Method
  - Rename Method
  - Introduce Assertion
history:
  - author: 'Marcel Jerzyk'
    type: 'origin'
    named_as:
      - '"What" Comment'
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
  - author: 'William C. Wake'
    type: 'mention'
    source:
      year: 2004
      authors:
        - William C. Wake
      name: 'Refactoring Workbook'
      type: 'book'
      href:
        isbn_13: '978-0321109293'
        isbn_10: '0321109295'
  - author: 'Martin Fowler'
    type: 'parentage'
    named_as:
      - Comments
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

## "What" Comment

Recognizing all comments as Code Smells is controversial and raises many different opinions. For this reason, I define a concrete subcategory of comments named _"What" Comments_ that clearly defines only these comments, which in the vast majority will hint at something smells. The rule is simple: If a comment describes _what_ is happening in a particular code section, it is probably trying to mask some other Code Smell.

This distinction leaves room for the _"Why" Comments_ which were already defined by Wake in 2004 and were considered helpful. Wake also notes that comments that cite non-obvious algorithms are also acceptable [[1](#sources)]. I wanted to mention that comments may have their places in a few more cases, such as extreme optimizations, note discussion conclusions for future reference after a code review, or some additional explanations in domain-specific knowledge.

The problem is that _Comments_ are generally smelly, as I have mentioned. This is because they are a deodorant for other smells [[2](#sources)]. They may also quickly degrade with time and become another category of comments [Fallacious Comments](./fallacious-comment.md), which are a rotten, misleading subcategory of [_"What" Comments_].

### Causation

The author sees that the code is confusing and tries to be helpful by adding explanations.

### Problems

#### **Duplication**

Bad Javadoc comments that duplicate the method name, parameter names, and types clutter the code. They can later become [Fallacious Comments](./fallacious-comment.md).

#### **Cover up for other smells**

A comment that must explain what is happening in the code indicates that it can't speak for itself, which is a strong indicator of present code smells.

### Examples

<div class="example-block">

_"What" Comment_ as a Grouping Label

#### Smelly

```java
class Foo {
    void run() {
        // ...

        // Creating report
        Report vanillaReport = getVanillaReport();
        Report tweakedReport = tweakReport(vanillaReport);
        Report finalReport = formatReport(tweakedReport);

        // Sending report
        sendReportToHeadquartersByEmail(finalReport);
        sendReportToDevelopersByChat(finalReport);
        // ...
    }
}
```

#### Solution

```java
class Foo {
    void run() {
        // ...
        Report report = createReport();
        sendReport(report);
    }

    private Report createReport() {
        Report vanillaReport = getVanillaReport();
        Report tweakedReport = tweakReport(vanillaReport);
        return formatReport(tweakedReport);
    }

    private void sendReport(Report report) {
        sendReportToHeadquartersByEmail(report);
        sendReportToDevelopersByChat(report);
    }
}
```

</div>

<div class="example-block">

_"What" Comment_ as a cover for [Uncommunicative Name](./uncommunicative-name.md) code smell.

#### Smelly

```java
double getGrossValue(double p, double t) {
    /*
     * p: price
     * t: tax
     */
    // ...
}
```

#### Solution

```java
double getGrossValue(double price, double tax) {
    // ...
}
```

</div>

<div class="example-block">

_"What" Comment_ as a cover for [Uncommunicative Name](./uncommunicative-name.md) code smell.

#### Smelly

Example of a useless Javadoc comment.

```java
/**
 * Increases attack by the given value.
 *
 * @param value the attack increase
 */
void increaseAttack(int value) {
    attack += value;
}
```

#### Counter example

Example of a more useful Javadoc comment.

```java
/**
 * Removes the character from the main game-world scene.
 * Logs a warning if another event already removed the character.
 */
void destroyCharacter(int characterId) {
    // ...
}
```

#### Solution

The only counter indication for removing the Javadoc comment would be an enforced auto-documentation requirement.

```java
void increaseAttack(int value) {
    attack += value;
}
```

</div>

### Refactoring:

- Extract Method
- Rename Method
- Introduce Assertion

---

##### Sources

- [[1](#sources)], [Parentage2] - William C. Wake, _"Refactoring Workbook"_ (2004)
- [[2](#sources)], [Parentage1] - Martin Fowler, _"Refactoring: Improving the Design of Existing Code"_ (1999)
- [Origin] - Marcel Jerzyk, _"Code Smells: A Comprehensive Online Catalog and Taxonomy"_ (2022)
