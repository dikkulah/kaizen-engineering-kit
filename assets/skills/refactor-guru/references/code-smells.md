# Refactoring.Guru Code Smells Reference

## 1. Bloaters (Excessive Size)
* **Long Method**: Function > 30 lines. *Fix: Extract Method, Replace Temp with Query.*
* **Large Class**: Class doing too many distinct things. *Fix: Extract Class, Extract Subclass.*
* **Primitive Obsession**: Using basic primitives (`String`, `i32`) instead of small domain objects (`MacAddress`, `Money`). *Fix: Replace Primitive with Object / Newtype.*
* **Long Parameter List**: Methods with $\ge 4$ arguments. *Fix: Introduce Parameter Object, Preserve Whole Object.*
* **Data Clumps**: Groups of fields always appearing together (e.g. `start_time, end_time`). *Fix: Extract Class / Struct.*

## 2. Object-Orientation Abusers
* **Switch Statements**: Complex conditional operators branching on type codes. *Fix: Replace Conditional with Polymorphism / Strategy.*
* **Temporary Field**: Variables that are only populated under certain edge cases. *Fix: Extract Class, Introduce Null Object.*
* **Refused Bequest**: Subclass inheriting methods it doesn't need or throws unsupported on. *Fix: Replace Inheritance with Delegation.*
* **Alternative Classes with Different Interfaces**: Classes doing identical work with different method names. *Fix: Unify via shared Interface / Trait.*

## 3. Change Preventers
* **Shotgun Surgery**: One change forces edits in dozens of distinct classes/files. *Fix: Move Method, Consolidate in shared abstraction (Ponytail Rule).*
* **Divergent Change**: One class is frequently changed in different ways for different reasons. *Fix: Split according to reasons for change (SRP).*
* **Parallel Inheritance Hierarchies**: Creating a subclass in one tree forces creating a matching subclass in another. *Fix: Collapse or delegate.*

## 4. Dispensables (Dead Weight)
* **Duplicate Code**: Identical or near-identical code blocks. *Fix: Extract Method, Form Template Method.*
* **Comments**: Comments used to explain bad, convoluted code. *Fix: Rewrite code to be clear, delete redundant comments.*
* **Dead Code**: Unreachable, uncalled functions or unused struct fields. *Fix: Delete immediately.*
* **Speculative Generality**: "We might need this someday" code. *Fix: Delete immediately (YAGNI).*

## 5. Couplers (Excessive Dependencies)
* **Feature Envy**: Method accessing data of another object more than its own. *Fix: Move Method to the owner of the data.*
* **Inappropriate Intimacy**: Classes knowing too many private implementation details of each other. *Fix: Hide Delegate, Move Method.*
* **Message Chains**: `a.getB().getC().getD().doIt()`. *Fix: Hide Delegate (Law of Demeter).*
* **Middle Man**: A class whose sole job is delegating to another class. *Fix: Remove Middle Man, call target directly.*

