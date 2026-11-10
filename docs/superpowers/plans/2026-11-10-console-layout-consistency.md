# Console Layout Consistency Implementation Plan

> **For agentic workers:** Implement this narrow visual correction inline, with browser regression checks before and after the CSS change. The user requested all-page alignment, smaller gutters, consistent radii, and continuation of the existing server deployment.

**Goal:** Align the header and content on all seven routes, use available desktop width, reduce horizontal page gutters, and unify surface/control radii without changing business behavior.

**Architecture:** Keep the existing native HTML and the approved teal console design. The shared `.content` owns outer gutters; `.app-bar` and `#main` fill that same inner width. Replace arbitrary component radii with existing tokens and clip the file rail at its enclosing panel corners without clipping menus or focus rings.

**Tech Stack:** Native HTML/CSS/JavaScript, existing Playwright page scenarios with intercepted APIs, Node.js interaction tests, Python service, SSH deployment.

---

## Diagnosis and visual specification

The header and main each have a 1240px cap, but main adds 24px horizontal padding inside its border box. Visible cards therefore sit 24px inside each header edge; the cap adds more unused space at large viewport sizes. The same issue exists on all routes, with 16px extra inset on phones. A global `main` selector also styles the independent login layout.

| Before | After | Why |
| --- | --- | --- |
| Header and content have independent caps and gutters | One fluid parent with 16px desktop/tablet gutters and 12px phone gutters | Shared left/right edges and more usable file/table space |
| Square header, 12px desktop cards and 8px phone cards | Header and major cards consistently 12px, 8px controls, existing pill/circle exceptions | Consistent component shapes across routes and devices |
| Miscellaneous 6/7/9/10/14/16px component radii | Reuse the existing 4/8/12px tokens for rectilinear surfaces | Avoid page-specific rounding drift |
| File rail extends into its panel bottom corner | Give the rail/workspace the appropriate inherited bottom corner | Preserve the outer panel silhouette without clipping overlays |

Keep existing colors, typography, navigation, mobile bottom sheets, touch target sizes, API contracts, saved credentials, and running transfers. Inputs remain accessible, full-width tables retain horizontal overflow inside their own container, and page navigation remains keyboard operable.

## Task 1: Reproduce and add browser assertions

- [x] Extend `tests/browser/console-smoke.js` to measure the header and route border boxes, shared gutters, major panel/control radii and horizontal overflow for seven routes at 320, 390, 767, 768, 1023, 1024, 1440, 1920 and 2560px in light/dark themes.
- [x] Run the scenario against a managed loopback static fixture server with all API requests intercepted; record the expected alignment/radius failures before editing CSS.

## Task 2: Correct shared layout and corner tokens

- [x] Update `web/style.css`: move outer horizontal spacing onto `.content`, remove the duplicated 1240px caps, scope main layout to `#main`, and keep matching header/content border boxes.
- [x] Make the header a 12px rounded panel with uniform border; keep sticky positioning and safe-area spacing functional at each breakpoint.
- [x] Normalize rectangular component radii to `--radius-sm`, `--radius`, `--radius-lg` or `--radius-xl`, keeping circular status, pill labels, progress bars and joined list rows intentional.
- [x] Correct file rail/workspace corner painting at desktop and mobile breakpoints.
- [x] Update `.ulpi/design/DESIGN.md` with the new fluid width, gutter and corner rules.

## Task 3: Verify and deploy

- [x] Run the browser scenario and inspect desktop/file, tablet/activity and mobile/file screenshots; inspect login, dialogs and scroll/sticky states.
- [x] Run `node --check web/app.js`, `node --check tests/browser/console-smoke.js`, `node --test tests/ui-interactions.js`, and `git diff --check`.
- [x] Document measured checks and limits in `docs/ui-layout-verification.md`.
- [x] Back up the current remote frontend, deploy changed frontend files to the configured static directory, verify returned bytes, existing service PIDs and service activity, and report the management URL.
