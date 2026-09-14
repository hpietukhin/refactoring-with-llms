---
slug: 'feature-envy'
meta:
  last_update_date: 2022-04-19
  title: 'Feature Envy'
  description: "A method that touches another class's fields more than its own. It was written in the wrong place and belongs closer to the data it can't stop reaching for."
  known_as:
    - ---
categories:
  expanse: 'Between'
  obstruction:
    - Couplers
  occurrence:
    - Responsibility
  tags:
    - ---
  smell_hierarchies:
    - Code Smell
    - Design Smell
relations:
  related_smells:
    - name: Fate over Action
      slug: fate-over-action
      type:
        - caused
    - name: Insider Trading
      slug: insider-trading
      type:
        - co-exist
problems:
  general:
    - Reusability
    - Low Testability
    - Bijection
  violation:
    principles:
      - Tell, Don’t Ask
    patterns:
      - ---
refactors:
  - Move Method
  - Move Field
  - Extract Method
history:
  - author: 'Martin Fowler'
    type: 'origin'
    named_as:
      - Feature Envy
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

## Feature Envy

If a method inside a class manipulates more features (be it fields or methods) of another class more than from its own, then this method has a _Feature Envy_. In Object-Oriented Programming, developers should tie the functionality and behavior close to the data it uses. The instance of this smell indicates that the method is in the wrong place and is more tightly coupled to the other class than to the one where it is currently located. [[1](#sources)]

This was the explanation based on Fowler's book from 1999. In his recent "book update", he rephrased the _class_ into _module_, generalizing the concept from a _zone_ perspective. Depending on the size of the system, the _Feature Envy_ code smell may apply accordingly.

### Causation

The root cause of this smell is misplaced responsibility.

### Problems

#### **Low Testability**

Difficult to create proper test or tests in separation. Mocking is required.

#### **Inability to Reuse**

Coupled objects have to be used together. This can cause lousy duplication issues if one tries to reuse applicable code by extracting and cutting off what he does not need.

#### **Bijection Problems**

Real-world domain concepts and their code representations drift apart when behavior lives far from the data it operates on.

### Example

<div class="example-block">

#### Smelly

```java
record ShoppingItem(String name, double price, double taxMultiplier) {}

class Order {
    double getBillTotal(List<ShoppingItem> items) {
        return items.stream()
                .mapToDouble(item -> item.price() * item.taxMultiplier())
                .sum();
    }

    List<String> getReceiptLines(List<ShoppingItem> items) {
        return items.stream()
                .map(item -> "%s: %.2f$".formatted(
                        item.name(), item.price() * item.taxMultiplier()))
                .toList();
    }

    String createReceipt(List<ShoppingItem> items) {
        String receipt = String.join("\n", getReceiptLines(items));
        return "%s%nBill: %.2f$".formatted(receipt, getBillTotal(items));
    }
}
```

#### Solution

```java
record ShoppingItem(String name, double price, double taxMultiplier) {
    double taxedPrice() {
        return price * taxMultiplier;
    }

    String receiptLine() {
        return "%s: %.2f$".formatted(name, taxedPrice());
    }
}

class Order {
    double getBillTotal(List<ShoppingItem> items) {
        return items.stream()
                .mapToDouble(ShoppingItem::taxedPrice)
                .sum();
    }

    List<String> getReceiptLines(List<ShoppingItem> items) {
        return items.stream()
                .map(ShoppingItem::receiptLine)
                .toList();
    }

    String createReceipt(List<ShoppingItem> items) {
        String receipt = String.join("\n", getReceiptLines(items));
        return "%s%nBill: %.2f$".formatted(receipt, getBillTotal(items));
    }
}
```

</div>

### Refactoring:

- Move Method
- Move Field
- Extract Method

---

##### Sources

- [[1](#sources)] - Mika Mäntylä, _"Bad Smells in Software - a Taxonomy and an Empirical Study"_ (2003)
- [Origin] - Martin Fowler, _"Refactoring: Improving the Design of Existing Code"_ (1999)
