# GoF Design Patterns & Modern Rust/Java Mapping

## 1. Creational Patterns
* **Factory Method**: Define an interface for creating an object, but let subclasses/constructors decide which class to instantiate.
  * *Rust*: Associated function `fn new()`, enum constructors (`from_str`, `from_u8`).
* **Abstract Factory**: Produce families of related or dependent objects without specifying concrete classes.
  * *Rust*: Trait defining factory methods with generic return types.
* **Builder**: Separate construction of a complex object from its representation.
  * *Rust*: `#[derive(bon::Builder)]` or manual builder with typestate validation.
* **Singleton**: Ensure a class has only one instance and provide global access.
  * *Rust*: `std::sync::OnceLock<T>` (never write global mutable state).
  * *Java*: Spring singleton-scoped `@Component` / `@Service`.

## 2. Structural Patterns
* **Adapter**: Convert the interface of a class into another interface clients expect.
  * *Rust*: `impl From<LegacyType> for TargetType` / `impl Into<TargetType>`.
* **Composite**: Compose objects into tree structures to represent part-whole hierarchies.
  * *Rust*: Recursive enums (`enum AstNode { Leaf, Node(Vec<AstNode>) }`).
* **Decorator**: Attach additional responsibilities to an object dynamically.
  * *Rust*: Newtype wrapper implementing the same trait (`struct LoggingClient<T: Client>(T)`).
* **Facade**: Provide a unified, simplified interface to a library or subsystem.
  * *Rust*: Storybook facade module (`//!` header, $<120$ lines of delegation).
* **Proxy**: Provide a surrogate or placeholder for another object to control access to it.
  * *Rust*: Singleflight coalescer ($14 \rightarrow 1$) and lazy cached fetchers.

## 3. Behavioral Patterns
* **Chain of Responsibility**: Pass requests along a chain of potential handlers.
  * *Rust*: Vector of middleware trait objects or combinators (`Option::and_then`).
* **Command**: Encapsulate a request as an object, thereby letting you parameterize clients with different requests.
  * *Rust*: Closures `Box<dyn FnOnce() -> Result<()>>` or explicit command structs.
* **Iterator**: Access elements of an aggregate object sequentially without exposing underlying representation.
  * *Rust*: `impl Iterator for MyCollection`.
* **Observer**: Define a one-to-many dependency so that when one object changes state, all dependents are notified.
  * *Rust*: Tokio `tokio::sync::broadcast` or `mpsc` channels.
* **State / Typestate**: Alter behavior when internal state changes.
  * *Rust*: **Compile-Time Typestate Pattern** (`struct Connection<State>; Connection<Disconnected>::connect() -> Connection<Connected>`).
* **Strategy**: Define a family of algorithms, encapsulate each one, and make them interchangeable.
  * *Rust*: `impl Trait` or `Box<dyn Trait>`, closures.
* **Template Method**: Define the skeleton of an algorithm in an operation, deferring some steps to subclasses.
  * *Rust*: Trait with default method implementations calling required primitive methods.
* **Visitor**: Represent an operation to be performed on the elements of an object structure.
  * *Rust*: Exhaustive `match` on closed enums (eliminates the need for double-dispatch visitor boilerplate!).

