# functional-testing: failure modes

Load on demand. Each line is a defect class seen in practice.

- Mock returns exactly what the code under test expects - the test only proves the mock.
- Assertion on a log line or call count instead of the persisted outcome.
- Fixture shared between tests so one failure cascades or hides.
- Time/randomness not pinned - flaky and unfalsifiable.
