# performance-testing: failure modes

Load on demand. Each line is a defect class seen in practice.

- Benchmark on a tiny dataset that hides the quadratic term.
- Caching added with no invalidation story.
- Averages reported where the tail is the problem.
- Optimization measured on a developer machine under unrelated load.
