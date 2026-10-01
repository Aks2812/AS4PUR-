Absolutely. One important distinction first:

> **The generated screenshots do not contain Figma/dev-mode metadata**, so I cannot truthfully claim that any hex code, font size, or spacing was *extracted* from the image.
>
> What I can give you is a **canonical design specification**: exact literal values that we deliberately define for AS4PUR. In other words, these are the values Code should implement, rather than values we are pretending to have measured from the screenshot.

That is actually better for consistency: **Landing and Login will share one AS4PUR design system.**

# AS4PUR UI Design Specification
**Version: 1.0**

## 1. Global Design Tokens

### Color palette

| Token | Hex | Usage |
|---|---|---|
| `--color-navy-900` | `#102A56` | Primary brand background |
| `--color-navy-800` | `#142F5F` | Secondary navy / subtle variation |
| `--color-navy-700` | `#1B3A70` | Decorative lines |
| `--color-navy-text` | `#102A56` | Primary text |
| `--color-blue-muted` | `#647A9E` | Secondary text |
| `--color-orange-500` | `#E97800` | Primary CTA / brand accent |
| `--color-orange-400` | `#F28A18` | Hover / accent variation |
| `--color-white` | `#FFFFFF` | Main background |
| `--color-surface` | `#F8F9FC` | Input background |
| `--color-border` | `#D9E0EC` | Input / subtle borders |
| `--color-border-light` | `#E8ECF3` | Dividers |
| `--color-text-primary` | `#102A56` | Headings |
| `--color-text-secondary` | `#647A9E` | Descriptions |
| `--color-text-muted` | `#8290A8` | Placeholder / metadata |
| `--color-error` | `#C62828` | Validation errors |

### Important

The **orange should remain the only strong accent**.

Don't introduce green, purple, cyan, gradients, etc.

---

# 2. Typography

I'd use **Inter** throughout the application.

If Inter isn't already available, use:

```css
font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
```

### Typography scale

| Token | Size | Weight | Line height |
|---|---:|---:|---:|
| Display | `42px` | 700 | `1.15` |
| H1 | `32px` | 700 | `1.2` |
| H2 | `24px` | 700 | `1.25` |
| H3 | `18px` | 600 | `1.35` |
| Body | `15px` | 400 | `1.6` |
| Body Medium | `15px` | 500 | `1.6` |
| Small | `13px` | 400 | `1.5` |
| Label | `13px` | 500 | `1.4` |
| Caption | `12px` | 400 | `1.4` |

---

# 3. Global Layout

Both pages should use the same fundamental split:

```text
┌──────────────────────┬─────────────────────────────────────┐
│                      │                                     │
│     BRAND PANEL      │            CONTENT PANEL            │
│                      │                                     │
│        36%           │                 64%                 │
│                      │                                     │
└──────────────────────┴─────────────────────────────────────┘
```

### Exact proportions

```css
--brand-panel-width: 36%;
--content-panel-width: 64%;
```

For a `1536px` viewport:

- Left: ~`553px`
- Right: ~`983px`

The split should be **viewport-based**, rather than a fixed `500px`, so it remains responsive.

---

# 4. LEFT BRAND PANEL

This is shared between **Landing + Login**.

```css
background: #102A56;
```

### Layout

```css
display: flex;
align-items: center;
justify-content: center;
```

Brand container:

```css
width: 100%;
max-width: 360px;
padding: 32px;
text-align: center;
```

### Logo

Recommended exact dimensions:

```css
width: 72px;
height: 72px;
```

The actual orange icon should be:

```text
#E97800
```

### Spacing

```text
Logo
↓ 20px
AS4PUR
↓ 16px
Tagline
```

### AS4PUR

```css
font-size: 30px;
font-weight: 700;
line-height: 1.2;
color: #FFFFFF;
letter-spacing: -0.5px;
```

### Tagline

Exact text:

> Automation System for Private App Definition, User Provisioning, and RTP

I'd keep it as **three visual lines**:

```text
Automation System for
Private App Definition, User
Provisioning, and RTP
```

CSS:

```css
font-size: 15px;
font-weight: 400;
line-height: 1.6;
color: #D5DEED;
max-width: 300px;
margin: 16px auto 0;
```

---

# 5. Subtle Brand Panel Decoration

This is optional but I recommend it because it ties the landing and login screens together.

Use thin geometric lines in:

```text
#1B3A70
```

Opacity:

```text
0.65
```

Stroke:

```text
1px
```

They should sit around the **edges/corners**, not behind the logo.

For example:

```text
╲
 ╲________________
                  │


                  │
             ____╱
          __╱
       __╱
```

Keep them extremely subtle.

---

# 6. LANDING PAGE

Right panel:

```css
background: #FFFFFF;
```

Layout:

```css
position: relative;
min-height: 100vh;
padding: 48px 56px;
```

Content max width:

```css
max-width: 900px;
```

---

## Landing header

At the top-right:

```text
🌐 English   ˅
```

Position:

```css
top: 32px;
right: 48px;
```

Font:

```css
font-size: 13px;
font-weight: 500;
color: #102A56;
```

This can later become an actual language selector.

---

# 7. Landing Main Content

I'd structure the content like this:

```text
WELCOME TO AS4PUR

Secure Access.
Automated.

AS4PUR helps you manage private app definitions,
user provisioning, and RTP with automation,
security, and efficiency.

[feature]
Private App Definition
Manage and secure your private applications
with ease.

[feature]
User Provisioning
Automate user access and lifecycle
management.

[feature]
RTP (Real-Time Protection)
Keep your environment secure with
real-time protection.

[              Go to Login →              ]
```

---

## Eyebrow

```css
font-size: 15px;
font-weight: 600;
letter-spacing: 0.2px;
color: #647A9E;
text-transform: uppercase;
```

Text:

```text
WELCOME TO AS4PUR
```

---

## Hero heading

```css
font-size: 44px;
font-weight: 700;
line-height: 1.12;
letter-spacing: -1.2px;
color: #102A56;
```

Text:

```text
Secure Access.
Automated.
```

Spacing:

```text
Eyebrow
↓ 14px
Heading
↓ 20px
Description
```

---

## Landing description

```css
font-size: 16px;
line-height: 1.65;
font-weight: 400;
color: #647A9E;
max-width: 520px;
```

---

# 8. Feature List

Each feature:

```text
[ 48×48 icon ]   Private App Definition
                 Manage and secure your private
                 applications with ease.
```

### Icon container

```css
width: 48px;
height: 48px;
border-radius: 50%;
background: #FFF3E6;
```

Orange icon:

```text
#E97800
```

### Feature title

```css
font-size: 15px;
font-weight: 600;
color: #102A56;
```

### Feature description

```css
font-size: 13px;
line-height: 1.5;
color: #647A9E;
```

### Feature spacing

```text
Feature 1
↓ 20px
Feature 2
↓ 20px
Feature 3
```

---

# 9. Landing CTA

This is one of the most important shared components.

```css
height: 48px;
width: 352px;
border-radius: 8px;
background: #E97800;
color: #FFFFFF;
```

Text:

```text
Go to Login  →
```

Typography:

```css
font-size: 15px;
font-weight: 600;
```

### Hover

```css
background: #F28A18;
```

### Active

```css
background: #D96D00;
```

### Transition

```css
transition: background-color 150ms ease;
```

No glow.

No gradient.

No giant shadow.

---

# 10. Landing Illustration

The illustration on the right side of the hero can remain.

But I would treat it as **secondary decoration**, not a core UI element.

Recommended:

```css
width: 360px;
max-width: 40%;
opacity: 0.9;
```

Its visual language should use:

- Navy `#102A56`
- Light blue-gray `#E8ECF3`
- Orange `#E97800`
- White

This makes it visually belong to the same application.

---

# 11. Landing Footer

Bottom:

```text
AS4PUR  |  Automation System for Private App Definition,
           User Provisioning, and RTP                         v1.0.0
```

Divider:

```css
border-top: 1px solid #E8ECF3;
```

Padding:

```css
padding-top: 24px;
```

Font:

```css
font-size: 11px;
color: #8290A8;
```

---

# 12. LOGIN PAGE

Now the important part:

**Login should reuse the exact same left panel.**

Don't create a second interpretation of the brand.

The user should immediately think:

> "Yep, this is the same AS4PUR application."

---

# 13. Login Right Panel

```css
background: #FFFFFF;
display: flex;
align-items: center;
justify-content: center;
padding: 48px;
```

Login container:

```css
width: 100%;
max-width: 560px;
```

I'd use:

```css
padding: 32px;
```

No heavy card.

No large shadow.

---

# 14. Login Heading

```text
Sign in
Access your account to continue to AS4PUR.
```

### `Sign in`

```css
font-size: 32px;
font-weight: 700;
line-height: 1.2;
letter-spacing: -0.6px;
color: #102A56;
```

### Description

```css
margin-top: 8px;
font-size: 15px;
color: #647A9E;
```

---

# 15. Username Field

Spacing:

```text
Heading
↓ 40px
Username label
↓ 8px
Input
```

### Label

```css
font-size: 13px;
font-weight: 600;
color: #102A56;
```

### Input

```css
height: 52px;
width: 100%;
background: #F8F9FC;
border: 1px solid #D9E0EC;
border-radius: 8px;
padding: 0 16px;
```

Placeholder:

```text
Enter your username
```

```css
font-size: 14px;
color: #8290A8;
```

---

# 16. Password Field

Spacing from username:

```text
Input
↓ 24px
Password label
↓ 8px
Input
```

Same dimensions:

```css
height: 52px;
border-radius: 8px;
```

Placeholder:

```text
Enter your password
```

Add the eye button on the right:

```text
[ 🔒  Enter your password                    ◉ ]
```

Eye button:

```css
width: 44px;
height: 44px;
```

No background.

---

# 17. Input Focus State

This is important for perceived quality.

Normal:

```css
border: 1px solid #D9E0EC;
```

Focus:

```css
border: 1px solid #E97800;
box-shadow: 0 0 0 3px rgba(233, 120, 0, 0.12);
```

This gives the orange brand color a functional purpose.

---

# 18. Login Button

Spacing:

```text
Password input
↓ 32px
Button
```

Exact:

```css
height: 52px;
width: 100%;
border-radius: 8px;
background: #E97800;
```

Text:

```text
Sign in  →
```

```css
font-size: 15px;
font-weight: 600;
color: #FFFFFF;
```

Hover:

```text
#F28A18
```

Active:

```text
#D96D00
```

---

# 19. Login Error State

I'd also define this now so Code doesn't invent one later.

For invalid credentials:

```text
Username
[____________________________]

Password
[____________________________]

⚠ Invalid username or password.

[           Sign in →          ]
```

Error:

```css
font-size: 13px;
color: #C62828;
```

Do **not** turn the entire page red.

Only the relevant field/message should communicate the error.

---

# 20. Login Loading State

When authentication is happening:

```text
[        ◌ Signing in...       ]
```

Button remains:

```text
background: #E97800;
```

Use a small spinner.

Don't change the entire page.

---

# 21. Responsive Behavior

This is important.

### Desktop

At:

```text
≥ 1024px
```

Use:

```text
36% / 64%
```

### Tablet

At:

```text
768px – 1023px
```

Use:

```text
40% / 60%
```

### Mobile

At:

```text
< 768px
```

Switch to:

```text
┌──────────────────────────┐
│                          │
│        AS4PUR            │
│                          │
├──────────────────────────┤
│                          │
│        Sign in           │
│                          │
│ Username                 │
│ [______________________] │
│                          │
│ Password                 │
│ [______________________] │
│                          │
│ [       Sign in →      ] │
│                          │
└──────────────────────────┘
```

The navy brand area becomes a **compact header**, rather than consuming half the phone screen.

---

# 22. The Most Important Shared Design Tokens

If you only want to give Code a small set of instructions, give them this:

```css
:root {
  /* Brand */
  --as4pur-navy: #102A56;
  --as4pur-navy-dark: #0D2348;
  --as4pur-navy-light: #1B3A70;

  --as4pur-orange: #E97800;
  --as4pur-orange-hover: #F28A18;
  --as4pur-orange-active: #D96D00;

  /* Surface */
  --as4pur-white: #FFFFFF;
  --as4pur-surface: #F8F9FC;

  /* Text */
  --as4pur-text: #102A56;
  --as4pur-text-secondary: #647A9E;
  --as4pur-text-muted: #8290A8;

  /* Border */
  --as4pur-border: #D9E0EC;
  --as4pur-border-light: #E8ECF3;

  /* Status */
  --as4pur-error: #C62828;

  /* Layout */
  --brand-width: 36%;
  --content-width: 64%;

  /* Radius */
  --radius-sm: 6px;
  --radius-md: 8px;
  --radius-lg: 12px;

  /* Spacing */
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 20px;
  --space-6: 24px;
  --space-8: 32px;
  --space-10: 40px;
  --space-12: 48px;
  --space-16: 64px;
}
```

---

# 23. Final relationship between the two pages

The design system should deliberately make them feel like **two states of the same application**:

```text
                    AS4PUR
                      │
          ┌───────────┴───────────┐
          │                       │
       LANDING                  LOGIN
          │                       │
     Brand Panel              Brand Panel
     Navy 36%                 Navy 36%
          │                       │
     Welcome                    Sign in
     Features                 Username
     Illustration             Password
     CTA                      CTA
          │                       │
          └───────────┬───────────┘
                      │
                SAME DESIGN TOKENS
```

So the user experience becomes:

**Landing**

> "This is what AS4PUR does."

↓

**Go to Login**

↓

**Login**

> "This is the same AS4PUR system; now authenticate."

That's the direction I'd use rather than making the landing page look like a completely separate marketing website.

### One correction to the generated mockup

I would **not blindly reproduce every pixel of the generated image**. The image is a visual concept, not a Figma specification. In particular, the exact illustration geometry and some text positioning are generated approximations.

The specification above is the **literal implementation contract** I'd give to Code. That means if Code follows these tokens and dimensions, we have a reproducible AS4PUR visual system rather than continuously eyeballing screenshots.

---

## Implementation Corrections & Notes (2026-09-16)

The spec above is preserved verbatim (copied from `design-spec-raw.md` at the
project root, which is a scratch/relay file subject to being overwritten by
future pastes - this file is the permanent record). The following
corrections and deviations were applied when implementing it into
`landing.html`/`login.html` - documented here so a future edit knows which
differences from the literal text above are deliberate, not oversights:

1. **Tagline (Sections 4 and 11) is superseded.** The literal spec text
   above ("Automation System for Private App Definition, User
   Provisioning, and RTP") is NOT what's implemented. Every place the
   tagline appears (brand panel, footer) uses instead: "Automation system
   for Private App Definition, User Provision, and RTP Creation" - per
   explicit operator correction, to stop using "-ing" endings that don't
   match the operation names used elsewhere in the app (sidebar, dashboard
   cards).

2. **Section 8 feature list, two copy corrections:**
   - "User Provisioning" → "User Provision" (matches the operation name
     used everywhere else in the app - sidebar, dashboard card, tagline -
     exactly, no `-ing`).
   - "RTP (Real-Time Protection)" is not used - it overstates scope. AS4PUR
     automates NPA (private access) policy creation specifically; SWG
     conversion is a separate, deliberately manual step done directly in
     the Netskope console (see CLAUDE.md Section 9's "Any Traffic" /
     "Block All Internet" limitation), outside this app. Replaced with:
     - Title: "RTP Creation"
     - Description: "Bulk-create Netskope Private Access policies in
       minutes, not hours - no more manual per-rule work in the Netskope
       console."

3. **Two additional landing page sections**, inserted after Section 10
   (illustration) and before Section 11 (footer), reusing the spec's own
   tokens with no new visual language:
   - **"The Challenge"** - eyebrow (Section 7's eyebrow token), H2 (spec's
     H2 token), a plain three-point Body-token list, deliberately no
     icons and no error/red color (kept visually calm, not alarming).
   - **"Security by Design"** - eyebrow, H2, and three feature items using
     the exact same icon-circle + title + description pattern as Section
     8 (48×48 circle, `#FFF3E6` background, orange icon - `#D9700F` since
     item 5's palette unification below, not the `#E97800` this section
     originally specified): dry-run review gates, full audit trail,
     role-based access.

4. **Font: unified onto the app's existing self-hosted Plus Jakarta Sans
   (corrected 2026-09-16; superseded the note that used to stand here).**
   The spec's Section 2 text itself sanctioned a fallback ("If Inter
   isn't already available, use: `"Inter", -apple-system,
   BlinkMacSystemFont, "Segoe UI", sans-serif`"), and landing/login
   briefly used that literal stack as its own font-family declaration.
   The operator then asked for full unification instead of a second font
   system existing alongside the app's real one: `.brand-shell`'s
   font-family override was removed entirely rather than swapped for
   another literal stack - `body {}` (style.css) already declares the
   app's self-hosted Plus Jakarta Sans, and normal CSS inheritance
   carries that into landing/login for free once nothing shadows it.
   Zero new font hosting, zero CSP surface added - this was a pure
   deletion, not a new declaration.

5. **Navy + orange unified onto the app's existing tokens (corrected
   2026-09-16; superseded the "known cross-page palette mismatch" note
   that used to stand here).** The spec's own `#102A56` (navy) and
   `#E97800` (orange, plus its `#F28A18` hover / `#D96D00` active
   variants) have been fully replaced, everywhere on landing.html/
   login.html/brand_panel.html/style.css, by the app's real,
   already-established tokens: `--color-bg-dark` (`#0E2148`) and
   `--color-accent` (`#D9700F`). This was done as alias substitution, not
   hardcoded re-typing - `.brand-shell`'s local `--as4pur-navy`/
   `--as4pur-text`/`--as4pur-orange` custom properties now read
   `var(--color-bg-dark)`/`var(--color-accent)` directly, so a future
   change to the app's real navy/orange values updates landing/login
   automatically instead of requiring a second edit. The orange's
   hover/active variants are the same kind of alias: `--as4pur-orange-
   active` now reads `var(--brand-dark)` (`#B85F0C` - already existed in
   the app's `:root`, used by `a:hover`/`.stacked-form button:hover`, and
   turned out to be `--color-accent`'s own channels × 0.85 exactly, so no
   new value was even needed there); `--as4pur-orange-hover` reads a
   newly-added `--brand-hover` (`#DE8533`) added to the app's real
   `:root` (not kept private to landing/login) - each of `--color-
   accent`'s channels lightened 15% toward white, the mirror-image of
   the darken formula `--brand-dark` already used. The `box-shadow`
   focus-ring rgba value (`rgba(233, 120, 0, 0.12)`) was likewise
   converted to `--color-accent`'s own RGB (`rgba(217, 112, 15, 0.12)`).
   `--as4pur-navy-light` (`#1B3A70`, Section 5's decorative corner-line
   color) and every non-navy/non-orange token (surface, border,
   text-secondary, text-muted, error) are unchanged - this correction
   was scoped to navy structural color + orange accent only, per the
   operator's explicit instruction, not a full palette merge. There is
   no more visible seam at the login → dashboard transition on these two
   colors - this is now one unified system, not two coexisting ones.

6. **Logo color - the existing `as4pur-mark.svg` is reused unmodified**
   (same file already used in the sidebar), never recolored. Its
   baked-in accent is `#D9700F` (hardcoded into the SVG's own `fill`/
   `stroke` attributes, not `currentColor`-driven) - originally flagged
   as a minor mismatch against this spec's own `#E97800`; now, after
   item 5's palette unification, the page's orange accent IS `#D9700F`
   too, so the logo and the page match exactly rather than approximately.
   **C2PA metadata claim, directly verified 2026-09-16** (an earlier pass
   asserted this from memory; re-checked against the actual file rather
   than left as an unconfirmed claim): `app/static/icons/as4pur-mark.svg`
   genuinely contains `xmlns:c2pa="http://c2pa.org/manifest"` and a
   `<metadata><c2pa:manifest>` element holding a real base64-encoded
   CBOR/JUMBF C2PA manifest - decoding it shows an X.509 certificate
   chain for "Anthropic, PBC" / "Anthropic Content Credentials Root CA" /
   "Anthropic Claude Content Signing", a `claim_generator_info` of
   "Anthropic Files" v1.0.0, and a human-readable assertion description
   ("Claude provided this file at the request of a user and may have
   created or modified the file contents."). The description was
   accurate, not imprecise - this file should still never be re-encoded
   or byte-edited, which is exactly why item 5's unification changed
   only the surrounding CSS/HTML, never this file itself.

7. **Illustration asset**: the two attached files
   (`AS4PUR_Landing_Page_Artefact.png` / `AS4PUR_Login_Page_Artefact.png`)
   are full-page mockup renders, not isolated illustration graphics - each
   contains a complete two-panel page composition including its own copy
   of on-page text. Both originals are saved unmodified under
   `app/static/images/` (`as4pur-landing-artefact.png` /
   `as4pur-login-artefact.png`) as the literal files requested. Only a
   crop of the isometric illustration graphic from the Landing artefact
   (`app/static/images/landing-illustration.png`) is actually referenced
   on-page, in Section 10's illustration slot - embedding either full
   mockup wholesale into that slot would visually duplicate the
   surrounding page's own text at a tiny size. The Login artefact isn't
   referenced from any page (the login page has no illustration slot in
   this spec) - it's kept as a saved reference image only.
