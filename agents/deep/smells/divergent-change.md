---
slug: 'divergent-change'
meta:
  last_update_date: 2022-04-19
  title: 'Divergent Change'
  description: 'A class that changes for database reasons on Monday, calculation reasons on Wednesday, and display reasons on Friday. Same file, different reasons, and the merge conflicts pile up every sprint.'
  known_as:
    - ---
categories:
  expanse: 'Within'
  obstruction:
    - Change Preventers
  occurrence:
    - Responsibility
  tags:
    - ---
  smell_hierarchies:
    - Code Smell
    - Design Smell
relations:
  related_smells:
    - name: Shotgun Surgery
      slug: shotgun-surgery
      type:
        - family
problems:
  general:
    - Duplication
  violation:
    principles:
      - Single Responsibility
    patterns:
      - ---
refactors:
  - Extract Superclass
  - Extract Subclass
  - Extract Class
  - Extract Function
  - Move Function
history:
  - author: 'Martin Fowler'
    type: 'origin'
    named_as:
      - Data Clump
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

## Divergent Change

If adding a simple feature makes the developer change many seemingly unrelated methods inside a class, that indicates the _Divergent Change_ code smell. Simply put, the class has irrelevant methods in it [[1](#sources)]. As an example, suppose that someone needs to modify class `A` due to a change in the database but then has to modify the same class `A` due to a change in the calculation formula [[2](#sources)].

The difference between _Divergent Change_ and [_Shotgun Surgery_](./shotgun-surgery.md) is that the _Divergent Change_ addresses the issue within a class, while the _Shotgun Surgery_ between classes.

### Causation

Over time, a class tries to do more and more things and has many responsibilities. The fact that the class already has two or more different types of decisions implemented (for example, finding an object and doing something with object [[3](#sources)]) was overlooked and left unrefactored.

### Problems

#### **Single Responsibility Principle Violation**

The class has too much responsibility.

#### **Duplication**

### Example

<div class="example-block">

#### Smelly

```java
class ReportModifier {
    private final ReportStore reportStore;

    ReportModifier(ReportStore reportStore) {
        this.reportStore = reportStore;
    }

    Report getReport(String reportName) {
        return reportStore.read(reportName);
    }

    Report modifyReport(Report report, String newEntry) {
        return report.withEntry(newEntry);
    }

    Report run(String reportName, String newEntry) {
        Report report = getReport(reportName);
        return modifyReport(report, newEntry);
    }
}

var reportModifier = new ReportModifier(reportStore);
var modifiedReport = reportModifier.run("report.csv", "Parsed");
```

#### Solution

```java
class ReportReader {
    private final ReportStore reportStore;

    ReportReader(ReportStore reportStore) {
        this.reportStore = reportStore;
    }

    Report getReport(String reportName) {
        return reportStore.read(reportName);
    }
}

class ReportModifier {
    Report modifyReport(Report report, String newEntry) {
        return report.withEntry(newEntry);
    }
}

var reportReader = new ReportReader(reportStore);
var report = reportReader.getReport("report.csv");

var reportModifier = new ReportModifier();
var modifiedReport = reportModifier.modifyReport(report, "Parsed");
```

</div>

### Refactoring:

- Extract Superclass, Subclass, or new Class
- Extract Function
- Move Function

---

##### Sources

- [[1](#sources)] - MS Haque, J Carver, T Atkison, "Causes, impacts, and detection approaches of code smell: A survey" (2018)
- [[2](#sources)] - Mika Mäntylä, _"Bad Smells in Software - a Taxonomy and an Empirical Study"_ (2003)
- [[3](#sources)] - William C. Wake, _"Refactoring Workbook"_ (2004)
- [Origin] - Martin Fowler, _"Refactoring: Improving the Design of Existing Code"_ (1999)
