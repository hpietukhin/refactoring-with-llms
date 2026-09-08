---
slug: 'required-setup-or-teardown-code'
meta:
  last_update_date: 2022-04-19
  title: 'Required Setup or Teardown Code'
  description: "Close the socket when you're done. Check the environment variables before you start. Reset the state after every call. The object could handle all of this internally. Instead, it made it your problem."
  known_as:
    - ---
categories:
  expanse: 'Between'
  obstruction:
    - Bloaters
  occurrence:
    - Responsibility
  tags:
    - ---
  smell_hierarchies:
    - Code Smell
relations:
  related_smells:
    - name: Afraid To Fail
      slug: afraid-to-fail
      type:
        - caused
    - name: Dubious Abstraction
      slug: dubious-abstraction
      type:
        - caused
    - name: Hidden Dependencies
      slug: hidden-dependencies
      type:
        - caused
    - name: Duplicated Code
      slug: duplicated-code
      type:
        - causes
problems:
  general:
    - Cohesion
  violation:
    principles:
      - ---
    patterns:
      - ---
refactors:
  - Implement AutoCloseable
  - Use try-with-resources
history:
  - author: 'Steve Smith'
    type: 'origin'
    named_as:
      - Required Setup or Teardown Code
    regarded_as:
      - Code Smell
    source:
      year: 2013
      authors:
        - Steve Smith
      name: 'Refactoring Fundamentals'
      type: 'course'
      href:
        direct_url: 'https://www.pluralsight.com/courses/refactoring-fundamentals'
---

## Required Setup or Teardown Code

If, after the use of a class or method, several lines of code are required to:

- set it properly up,
- the environment requires specific actions beforehand or after its use,
- clean up actions are required,

then there is a _Required Setup or Teardown Code_ code smell. Furthermore, this may indicate [improper abstraction level](./dubious-abstraction.md).

### Causation

Some functionality was taken beyond the class during development, and the need for their use within the class itself was overlooked.

### Problems

#### **Lack of Cohesion**

Class can't be reused by itself - it requires extra lines of code outside of its scope to use it.

### Examples

<div class="example-block">

#### Smelly

```java
class Radio {
    final Socket socket;

    Radio(String host, int port) throws IOException {
        socket = new Socket(host, port);
    }

    // ...
}

var radio = new Radio(host, port);
// Use the radio.
radio.socket.shutdownOutput();
radio.socket.close();
```

#### Solution

```java
class Radio implements AutoCloseable {
    private final Socket socket;

    Radio(String host, int port) throws IOException {
        socket = new Socket(host, port);
    }

    @Override
    public void close() throws IOException {
        socket.close();
    }

    // ...
}

try (var radio = new Radio(host, port)) {
    // Use the radio.
}
```

</div>

### Refactoring:

- Implement `AutoCloseable`
- Use try-with-resources

---

##### Sources

- [Origin] - Steve Smith, _"Refactoring Fundamentals"_ (2013)
