---
slug: 'combinatorial-explosion'
meta:
  last_update_date: 2022-04-19
  title: 'Combinatorial Explosion'
  description: 'Dozens of methods that do almost the same thing, each differing by one small detail. Add a new feature and the count multiplies again. Good luck remembering which variant handles which edge case.'
  known_as:
    - ---
categories:
  expanse: 'Within'
  obstruction:
    - Bloaters
  occurrence:
    - Responsibility
  tags:
    - ---
  smell_hierarchies:
    - Code Smell
    - Design Smell
relations:
  related_smells:
    - name: Conditional Complexity
      slug: conditional-complexity
      type:
        - family
    - name: Parallel Inheritance Hierarchies
      slug: parallel-inheritance-hierarchies
      type:
        - family
problems:
  general:
    - ---
  violation:
    principles:
      - Don't Repeat Yourself
      - Open Closed
    patterns:
      - Decorator
      - Strategy
      - State
refactors:
  - Replace Inheritance with Delegation
  - Tease Apart Inheritance
history:
  - author: 'William C. Wake'
    type: 'origin'
    named_as:
      - Combinatorial Explosion
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

## Combinatorial Explosion

The Combinatorial Explosion occurs when a lot of code does almost the same thing - here, the word "almost" is crucial. The number of cases needed to cover every possible path is massive, as is the number of methods. You can grasp a solid intuition of this smell by thinking about code blocks that differ from each other only by the quantities of data or objects used in them. Wake specifies that "(...) this is a relative of [Parallel Inheritance Hierarchies](./parallel-inheritance-hierarchies.md) Code Smell, but everything has been folded into one hierarchy." [[1](#sources)].

### Causation

Instead, what should be an independent decision, gets implemented via a hierarchy. Let us suppose that someone organized the code so that it queries an API by a specific method with specific set-in conditions and data. Sooner or later, there are just so many of these methods as the need for different queries increases in demand.

### Problems:

#### **Don't Repeat Yourself Principle Violation**

Introducing new functionality requires multiple versions to be introduced in various places.

#### **Open-Closed Principle Violation**

The design is not closed for modification when each new dimension requires classes for every combination.

### Example

<div class="example-block">

#### Smelly

```java
abstract class ReportPublisher {
    abstract void publish(Report report);
}

class EmailPdfReportPublisher extends ReportPublisher {
    @Override
    void publish(Report report) {
        new EmailDestination().deliver(new PdfRenderer().render(report));
    }
}

class EmailHtmlReportPublisher extends ReportPublisher {
    @Override
    void publish(Report report) {
        new EmailDestination().deliver(new HtmlRenderer().render(report));
    }
}

class ArchivePdfReportPublisher extends ReportPublisher {
    @Override
    void publish(Report report) {
        new ArchiveDestination().deliver(new PdfRenderer().render(report));
    }
}

class ArchiveHtmlReportPublisher extends ReportPublisher {
    @Override
    void publish(Report report) {
        new ArchiveDestination().deliver(new HtmlRenderer().render(report));
    }
}
```

#### Solution

```java
interface ReportRenderer {
    byte[] render(Report report);
}

interface ReportDestination {
    void deliver(byte[] content);
}

record ReportPublisher(
        ReportRenderer renderer,
        ReportDestination destination) {

    void publish(Report report) {
        destination.deliver(renderer.render(report));
    }
}

var emailPdf = new ReportPublisher(new PdfRenderer(), new EmailDestination());
var archiveHtml =
        new ReportPublisher(new HtmlRenderer(), new ArchiveDestination());
```

</div>

### Refactoring

- Replace Inheritance with Delegation
- Tease Apart Inheritance
- It's pretty hard to fix, as the existence of this Code Smell (Design Smell) occurs as soon as the system's design is decided.

##### Sources

- [[1](#sources)], [Origin] - William C. Wake, _"Refactoring Workbook"_ (2004)
