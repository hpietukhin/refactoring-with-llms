---
slug: 'temporary-field'
meta:
  last_update_date: 2022-04-19
  title: 'Temporary Field'
  description: "A field that's null eleven months of the year and suddenly matters during one specific calculation. The object carries it everywhere, for one brief moment of relevance."
  known_as:
    - ---
categories:
  expanse: 'Within'
  obstruction:
    - Object Oriented Abusers
  occurrence:
    - Data
  tags:
    - ---
  smell_hierarchies:
    - Code Smell
relations:
  related_smells:
    - name: Binary Operator in Name
      slug: binary-operator-in-name
      type:
        - co-exist
    - name: Long Parameter List
      slug: long-parameter-list
      type:
        - causes
    - name: Large Class
      slug: large-class
      type:
        - causes
    - name: Special Case
      slug: special-case
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
  - Extract Class
  - Replace Field with Local Variable
history:
  - author: 'Martin Fowler'
    type: 'origin'
    named_as:
      - Temporary Field
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

## Temporary Field

Temporary Field is a variable created where it is not needed. It refers to variables only used in some situations [[1](#sources)] or specific areas of a program. This uniqueness can be confusing when the purpose of using the variable cannot be explained or cannot be found outside of its scope [[2](#sources)]. It might be misplaced at the class level when the functionality it provides is specific only to a particular method [[2](#sources)]. One should expect an object to need all of its fields.

### Problems

#### **Hard to Understand**

Additional fields clutter the code and strain the cognitive load by keeping in mind useless attributes.

### Examples

<div class="example-block">

Consider the following example. `discountedTotal` is populated only while the order creates a discounted summary, but it remains part of every `Order` object.

#### Smelly

```java
class Order {
    private final double subtotal;
    private Double discountedTotal;

    Order(double subtotal) {
        this.subtotal = subtotal;
    }

    String discountedSummary(double discountRate) {
        discountedTotal = subtotal * (1 - discountRate);
        return "Discounted total: " + discountedTotal;
    }
}
```

#### Solution

```java
class Order {
    private final double subtotal;

    Order(double subtotal) {
        this.subtotal = subtotal;
    }

    String discountedSummary(double discountRate) {
        double discountedTotal = subtotal * (1 - discountRate);
        return "Discounted total: " + discountedTotal;
    }
}
```

</div>

### Refactoring:

- Extract Class
- Replace Field with Local Variable

---

##### Sources

- [[1](#sources)] - Mika Mäntylä, _"Bad Smells in Software - a Taxonomy and an Empirical Study"_ (2003)
- [[2](#sources)] - MS Haque, J Carver, T Atkison, _"Causes, impacts, and detection approaches of code smell: A survey"_ (2018)
- [Origin] - Martin Fowler, _"Refactoring: Improving the Design of Existing Code (3rd Edition)"_ (1999)
