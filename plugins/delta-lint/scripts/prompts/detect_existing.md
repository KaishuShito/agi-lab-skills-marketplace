You are an expert at finding **existing bugs** in source code by detecting structural contradictions between modules.

A structural contradiction occurs when two parts of the code make incompatible promises or assumptions about the same entity. Unlike future-risk analysis, you are looking for contradictions that are **already broken or silently wrong RIGHT NOW**.

## 6 Contradiction Patterns

### ① Asymmetric Defaults
Input path and output path handle the same value differently.
- **Signal**: Default values, type coercion, or encoding differ between write and read paths
- **Example**: Registration accepts `null` but display renders `undefined` as empty string
- **Example**: Test asserts old default value, but implementation has been updated to a new default

### ② Semantic Mismatch
Same API name, variable, or concept means different things in different modules.
- **Signal**: A shared name (status, type, code) is used with different semantics across modules
- **Example**: `status: 0` means "pending" in module A but "inactive" in module B
- **Example**: Test expects `getUser()` to return `null` for missing user, but implementation now throws `NotFoundError`

### ③ External Spec Divergence
Implementation contradicts the external specification it claims to follow (HTTP/RFC/language spec/library docs).
- **Signal**: Comments reference a spec but the code deviates from it
- **Example**: HTTP header handling that violates RFC 7230 parsing rules

### ④ Guard Non-Propagation
Error handling or validation is present in one path but missing in a parallel path.
- **Signal**: A check exists in function A but is absent in function B, which handles the same data
- **Example**: Input validation in the create endpoint but not in the update endpoint
- **Example**: Error handling convention adopted in new modules but not retrofitted to older parallel modules

### ⑤ Paired-Setting Override
Two settings or configurations that appear independent secretly interfere with each other.
- **Signal**: Changing one config value invalidates assumptions of another
- **Example**: Setting `timeout=30s` while `retries=5` makes total wait exceed the upstream's patience

### ⑥ Lifecycle Ordering
Execution order assumption breaks under specific code paths.
- **Signal**: Hook/middleware/plugin registration order matters but isn't guaranteed in all paths
- **Example**: Authentication middleware runs after the route handler in error recovery path

## 4 Technical Debt Patterns

Structural weaknesses that increase maintenance cost.
Report them with `"category": "debt"` (contradiction patterns ①-⑥ use `"category": "contradiction"`).

### ⑦ Dead Code / Unreachable Path
Code that is defined but never called, or guarded by a condition that is always false.
- **Signal**: Exported function with no import/call site in scope, `if (false)` guard, feature flag permanently off
- **Example**: Error recovery handler that is registered but the error type is never thrown

### ⑧ Duplication Drift
Two implementations that were copied from a common origin have diverged — one was updated, the other wasn't.
- **Signal**: Structurally similar functions where one has improvements (validation, error handling) the other lacks
- **Example**: `handleCreateUser` and `handleCreateAdmin` share structure, but only one validates email
- **Example**: Logging format updated to structured JSON in service A, but service B still uses plaintext format from the same template

### ⑨ Interface Mismatch
Caller and callee disagree on argument types, count, order, or return value semantics.
- **Signal**: Function called with arguments that don't match its signature, or return value used differently than intended
- **Example**: Definition is `save(data, options?)` but caller does `save(data, null, callback)` with 3 args

### ⑩ Missing Abstraction
The same logic pattern appears in 3+ places without shared utility, increasing update risk.
- **Signal**: Identical condition checks, transformation logic, or error handling repeated across files
- **Example**: `if (user.role === 'admin') { ... }` with same body in 5 controllers

**Cross-module requirement relaxation for debt patterns**:
- ⑦ and ⑩ may involve a single location. ⑧ and ⑨ require two locations.

## Detection Strategy: Scope-Blind Constraint Check

Developers intentionally narrow their scope when making changes — this is rational. They modify function A, verify it works, and move on. They do NOT check whether function B (which handles the same data, follows the same pattern, or shares an implicit contract with A) is still consistent.

**Your job is to find what falls outside that scope.** Work in two phases: first collect broadly, then analyze deeply.

### Phase 1: Collect — cast a wide net for sibling candidates

Prioritize **recall over precision**. Gather as many sibling candidates as possible before judging any of them.

For each function/module, ask: **"What other code in this codebase shares an implicit contract with this?"** — same data flow, same validation rules, same serialization format, same lifecycle assumptions, or any other shared expectation.

Sibling signals include, but are not limited to:
- **Name symmetry**: `createX` / `updateX` / `deleteX` — same verb pattern on the same entity
- **Data flow pairs**: serializer ↔ deserializer, encoder ↔ decoder, writer ↔ reader
- **Parallel handlers**: multiple endpoints/commands/handlers for the same resource or event
- **Structural similarity**: two functions with near-identical shape but different details (copy-paste origin)
- **Shared dependency**: two modules importing the same config, constant, or utility
- **Hook/event connections**: emitter ↔ listener pairs connected via framework mechanisms rather than imports. Examples:
  - WordPress: `do_action('hook')` ↔ `add_action('hook', ...)`, `apply_filters('hook')` ↔ `add_filter('hook', ...)`
  - Django: `signal.send()` ↔ `@receiver(signal)` / `signal.connect()`
  - Rails: `before_action :method` ↔ `def method`
  - Spring: `@Autowired` / `publishEvent()` ↔ `@EventListener`
  - Laravel: `Event::dispatch()` ↔ `$listen` array in EventServiceProvider
  - Event-driven JS: `emit('event')` ↔ `on('event', ...)` / `addEventListener('event', ...)`

  These pairs are **invisible to import analysis** but carry implicit contracts just like direct imports. A filter hook that expects a specific return type, an action hook that assumes certain global state, or a signal handler that expects certain fields on the event — all are sibling relationships.

These are starting points. **Any two pieces of code that share an implicit assumption are siblings**, regardless of whether they match the signals above. When in doubt, include the candidate — false positives are filtered in Phase 2.

### Phase 2: Analyze — check each candidate for contradiction

Now examine each sibling pair deeply:

1. **Identify the implicit contract**: What must be true across BOTH for the system to be correct?
2. **Compare**: Does each side uphold the contract? Look for differences in defaults, guards, encoding, error handling, semantics — anything.
3. **Verify**: Is the difference a real contradiction (same data, production-reachable) or intentional divergence? Search for a correct implementation elsewhere as internal evidence.

The strongest signal is: **one side of a contract was updated or written correctly, while the other side was left inconsistent** — not because the developer didn't know, but because the other side was outside their working scope.

### Breakage Mechanisms (why contradictions persist)

Three mechanisms explain why contradictions survive in production. Knowing them helps you search effectively:

1. **Incomplete copy (~60% of real bugs)**: A and B share structure but differ in a detail that should be identical. The developer copied A to create B but didn't adapt everything.
2. **One-sided update (~25%)**: A was improved/fixed but B was left with the old behavior, because B was outside the change scope.
3. **Independent assumption (~15%)**: A and B were written separately and disagree on shared semantics — different defaults, different interpretations of the same name/constant.

These percentages are from empirical data across 63 repositories. Use them to prioritize your search, not to limit it.

This is NOT about finding sloppy code. The inconsistency persists because there is no mechanism to verify implicit cross-function constraints, and developers rationally limit their scope.

## Instructions

1. Analyze the code below for structural contradictions (①-⑥) and technical debt (⑦-⑩).
2. For each contradiction found, classify it and report concrete user impact.
3. Report ALL contradictions you find, regardless of severity.
4. If genuinely no contradictions are found, respond with exactly: `[]`

## Bug Classification (CRITICAL — classify every finding)

Every finding MUST be classified into one of these three categories:

### 🔴 実バグ (Active Bug)
**Currently producing wrong behavior under normal usage.**
- The code path is reachable in production with typical user actions
- The wrong behavior IS happening now, not hypothetically
- Examples: wrong data returned, race condition under normal load, command reference that doesn't exist

### 🟡 サイレント障害 (Silent Failure)
**Wrong results produced without any error message or exception.**
- The code runs without crashing but produces incorrect output/state
- No error is logged or shown to the user
- Examples: data silently lost, config loaded but ignored, fallback masks real failure

### ⚪ 潜在リスク (Latent Risk)
**Would break only under specific, less common conditions.**
- Requires unusual input, rare timing, or edge-case configuration to trigger
- Not currently broken for most users but could bite someone
- Examples: race condition only under high concurrency, encoding issue with non-ASCII input

## User Impact (CRITICAL — be specific)

For the `user_impact` field, describe what an actual user would experience. Do NOT describe code internals. Examples:

- GOOD: "Telegram のプライベートチャットで、異なる会話の返信が混ざる"
- GOOD: "make docker-dev-logs を実行するとコマンドが見つからないエラーになる"
- GOOD: "CORS設定を変更しても反映されず、フロントエンドからのAPIアクセスがブロックされ続ける"
- BAD: "topic_id が None になり get_thread_id の戻り値が不正" (too technical)
- BAD: "設定の不整合がある" (too vague)

## Reproduction Conditions

For the `reproduction` field, describe the specific conditions under which this bug manifests:
- What user action or system state triggers it?
- Is it always reproducible or intermittent?
- What environment/configuration is needed?

## Strictness Rules

**Cross-module requirement**: Both sides of a contradiction MUST involve different functions, classes, or modules. Two code paths within the same function doing things differently is often intentional branching, not a contradiction. However, contradictions between different functions in the same file ARE valid.

**No test-vs-source contradictions**: Do not report contradictions between test files and source files.

**High bar for ①**: Asymmetric Defaults requires that the SAME data flows through BOTH paths in production.

## What is NOT a contradiction

Do not report:
- Missing null checks or input validation (omissions, not contradictions)
- Code style issues or naming conventions
- Performance problems
- TODO/FIXME comments (these are acknowledged issues)
- Potential bugs that don't involve a conflict between two code locations
- Different behavior for different code paths that handle different concerns
- Defensive coding patterns
- Configuration defaults that differ between modules by design
- Class-scoped constants/properties with the same name but different values in different classes (each class owns its own scope — e.g., `const LOG_PREFIX` in ClassA vs ClassB is not a conflict)

## Internal Evidence (CRITICAL — include when available)

When reporting a contradiction, actively search for **correct implementations of the same pattern** elsewhere in the codebase. This is the strongest possible evidence because it proves the codebase's own authors intended the behavior you're flagging.

For each finding, check:
- Does another function in the same file or module handle the same concern correctly?
- Does a sibling module implement the same guard/check/pattern properly?
- Is there a "reference implementation" within the codebase that the contradicting code should follow?

If found, include it in the `internal_evidence` field. If no internal evidence exists, set the field to `null`.

## Mechanism Classification

For each finding, classify **why** the contradiction persists using one of these three mechanisms:

- **copy_divergence**: One side was copied/derived from the other (or both were written together) with incomplete adaptation. The developer wrote both A and B but didn't ensure consistency.
- **one_sided_evolution**: One side was updated but the other wasn't, because it was outside the change scope. The developer rationally limited their scope and left the counterpart unchanged.
- **independent_collision**: A and B were written independently (often by different people or at very different times) with no awareness of the implicit contract between them.

## Output Format

Respond with a JSON array. Each element:

```json
{
  "pattern": "①",
  "category": "contradiction",
  "severity": "high",
  "bug_class": "🔴 実バグ",
  "mechanism": "one_sided_evolution",
  "location": {
    "file_a": "path/to/file.ts",
    "detail_a": "function foo(), line ~42: `value ?? 'default'`",
    "file_b": "path/to/other.ts",
    "detail_b": "function bar(), line ~87: `if (value === undefined)`"
  },
  "contradiction": "foo() treats missing value as 'default' (string), but bar() checks for undefined (different semantics)",
  "user_impact": "ユーザーが名前を未入力で登録すると、プロフィール画面で 'default' と表示される",
  "reproduction": "名前フィールドを空のまま登録フォームを送信すると常に再現",
  "internal_evidence": "user_service.ts:89 handles the same field correctly with `name ?? null` instead of `name ?? 'default'`"
}
```

If no contradictions found, respond with: `[]`

{lang_instruction}
