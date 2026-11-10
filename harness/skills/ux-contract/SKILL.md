---
name: ux-contract
description: Specify and review user-facing flows, screens, states and accessibility so a task can be completed by every user. Use when a task adds or changes screens, forms, navigation, dialogs, dynamic content or copy.
---

# ux-contract

## Apply when
A task changes what the user sees or does: flows, screens, states, UI components, and anything with forms, navigation, dialogs, dynamic content or media.

## UX design
1. Trace discoverability and hierarchy; walk the happy path and the alternates.
2. Cover loading, empty, error, partial, success, destructive-confirm, recovery/undo and offline states.
3. Check copy and trust cues (what will happen, what happened), consistency with existing patterns, responsive behavior, keyboard and focus order.
4. Judge task completion, not appearance.

## UI specification
1. Start from the task's journeys and acceptance criteria; read the existing UI and design tokens/components first and reuse them.
2. Specify per screen: purpose, layout hierarchy, every state (empty, loading, error, success, permission-denied, offline), copy, primary/secondary actions.
3. Define reusable component contracts (props/inputs, variants, events) and responsive behavior at phone and desktop widths.
4. Mark open design questions with an owner; do not decide product behavior silently.

## Accessibility
1. Keyboard reachability, focus order and visible focus; no traps.
2. Semantics, roles, accessible names and labels.
3. Contrast and reflow at 200-400% zoom; touch target size; reduced motion.
4. Screen-reader announcement for dynamic updates and errors; locale, RTL and long strings.
5. Error identification and recovery.

## Required output
Journey walk-through with findings per state, severity by task-blocking impact. UI spec: journey list, component inventory, open questions. Barrier list: who is blocked, which task, criterion (WCAG), fix, how to verify (keyboard walk, screen reader, axe).

## Do not
Demand redesign without a stated user impact; skip error and empty states; invent a new design system when one exists; treat a passing automated scan as proof of task completion; list barriers without user impact.

## References
`references/ux-failure-modes.md`, `references/a11y-failure-modes.md` (read only when the work hits one of them).
