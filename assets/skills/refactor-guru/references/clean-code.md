# Robert C. Martin (Uncle Bob) Clean Code Heuristics

## 1. Meaningful Names
* **Use Intention-Revealing Names**: The name should tell you why it exists, what it does, and how it is used.
* **Make Meaningful Distinctions**: Do not use noise words (`Info`, `Data`, `Manager`, `theMessage`).
* **Use Pronounceable & Searchable Names**: Single-letter variables should only be used for small local loop indexes ($i, j$).
* **Class Names**: Should be nouns or noun phrases (`Customer`, `WifiLinkResolver`), never verbs.
* **Method Names**: Should contain verbs or verb phrases (`calculateScore`, `fetchTopology`, `saveState`).

## 2. Functions
* **Small**: Functions should be small (ideally $< 25$ lines).
* **Do One Thing**: Functions should do one thing, do it well, and do it only.
* **One Level of Abstraction per Function (SLAP)**: Statements within a function should all be at the same level of conceptual abstraction.
* **Fewer Arguments**: Ideal function arguments: niladic (0), monadic (1), dyadic (2). Triadic (3) only when necessary. $\ge 4$ requires grouping into a Parameter Object.
* **No Side Effects**: A function should not secretly modify hidden variables or system state while calculating a value.
* **Command Query Separation (CQS)**: Functions should either do something (command) or answer something (query), but not both.

## 3. Error Handling
* **Prefer Exceptions / Typed Result over Return Codes**: Magic integer error codes clutter callers with defensive `if (code == -1)` checks.
* **Provide Context with Errors**: Always attach context (e.g. device serial, timestamp, endpoint name) to the failure.
* **Don't Return or Pass Null**: Returning `null` creates boilerplate `if (x != null)` checks and NullPointerExceptions. Return `Option<T>` / `Optional<T>` or empty collections.

## 4. Class Design & Boundaries
* **Classes Should Be Small**: Measured by responsibilities, not just line count.
* **Single Responsibility Principle (SRP)**: A class or module should have one, and only one, reason to change.
* **Cohesion**: Classes should have a small number of instance variables, with each method manipulating one or more of them.
* **Depend on Abstractions**: Code against interfaces/traits, not concrete classes.

