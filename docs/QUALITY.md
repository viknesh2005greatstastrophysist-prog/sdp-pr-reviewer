# Quality bar for this project (read before building or reporting)

Drawn from Vik's earlier projects (AURA capstone, Boreal Air site). These are rules, not tips.

1. "Not checked" means "not ready". Nothing is reported as done while any item is unverified.
   A hand-back lists exactly what was run and seen, and what was not.
2. Press the real thing. A feature is verified by using it the way a user does (start the
   server, click through the dashboard in a browser, call the endpoint), not by reading code
   or trusting a unit test alone. Check desktop (1440 px) and phone (390 px) widths.
3. A check you wrote for your own work is trusted only after it has been seen to FAIL on a
   known fault (break the code, watch the test go red, restore it).
4. Every fix can break something elsewhere: re-run the full suite, lint and the browser pass
   after every round, not only the tests near the change.
5. A cold reviewer who did not build the work reviews it before it is shown. Fix, then
   review again. The builder does not grade their own work.
6. No fabricated numbers. Evaluation metrics, latency and cost come from real runs with the
   raw output saved. If something could not be measured, say so plainly.
7. No empty or dead UI: no button that does nothing, no blank panel without an explanation,
   no console errors, no horizontal scroll, every interactive element has a visible hover and
   keyboard focus state, every error the API can return is shown to the user in plain words.
8. Secrets never leave the server: never printed, logged, committed or sent to the browser.
9. Advisory only: the system never approves or merges a pull request; publishing comments
   needs the server permission AND an explicit per-review opt-in.
10. Reports are short and labelled: verified / not verified / not done, with the commit.
