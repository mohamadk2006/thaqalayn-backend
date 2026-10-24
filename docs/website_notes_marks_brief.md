# Website developer brief 2: notes, highlights, numbered marks, folders, study sessions

For the developer of www.thaqalaynlibrary.com. It adds to `docs/website_developer_brief.md`
(brief 1: sign-in, sync, meaning search, AI chat). Read brief 1 sections 0 to 2 first: this brief
assumes the sign-in and the sync engine (pull, push, cursor, tombstones, baseSeq) exist, and only
adds what is specific to these record kinds.

Everything here was taken from the iPhone app's code (its own document
`docs/notes_and_marks_behavior.md`, written 2026-10-24) and checked against the synced data on the
server. It describes what the app **does today**. Where the app has a known bug or a temporary choice
it says so, and what the website should do instead. The goal is that the website and the phone can
edit the same notes, marks and sessions without breaking each other's data.

The JSON examples in section 9 were built by hand from the app's mapping code, not captured from a
live account. The shapes are exact; the ids, texts and times are made up.

---

## 0. What to build, in what order

1. **Read-only first.** After sync, show the user's notes, folders, highlights, numbered marks,
   sessions and positions exactly as the phone shows them. This proves your reading of the data and
   catches position errors early, without any risk to the user's data.
2. **Notes editing:** create, edit, rename, move, reorder, delete; folders.
3. **Highlights and numbered marks in the reader:** create, change, remove.
4. **Study sessions tab:** create, add books, positions, delete.

Do not ship step 2 to users before step 1 works against real synced data from a phone.

Vocabulary used below (same as the app):

- **Paragraph / block:** one `text` or `footnotes` block of a page, from
  `GET /books/{id}/pages/{sequence}` → `page.blocks[]`. Its `id` (for example `p-000052-b-001`; the
  first number is the page sequence) is the `paragraphId` / `locationId` in the data.
- **Piece:** `{paragraphId, start, end}`, one paragraph's slice of a selection.
- **Scope:** a thing made in the main library has no `sessionId`; a thing made inside a study session
  has that session's `sessionId` and belongs only to it. The main library never shows a session's
  notes, highlights, marks or bookmarks, and a session never shows the library's.

---

## 1. Highlight, numbered mark, note: the differences

| | Highlight («تظليل») | Numbered mark («إضافة رقم») | Note |
|---|---|---|---|
| What it is | a coloured background on some text | a round number badge in the margin plus a grey wash on the text, tied to a numbered line `N. …` in a note | a plain-text document, optionally in a folder |
| Colours | yellow, green, blue, pink, purple | one grey wash, one badge colour | none |
| Has a note | never | always (`noteId`) | – |
| Sync kind | `highlight` (one record per paragraph piece) | `numberedMark` (one record, several `spans`) | `note`, `noteFolder` |
| Screens | «كل التظليلات» (in a session «التظليلات») | «كل الإشارات المرقّمة» (in a session «الإشارات المرقّمة») | «الملاحظات», folders «المجلدات» |

A highlight has no note and cannot get one. A numbered mark is how a place in a book is linked to a
numbered line in a note: the line `3. my comment` in a note and the badge `3` in the book belong
together; tapping either one opens the other.

---

## 2. Text positions (the part that goes wrong most often)

- `start`, `end`, `offset` are **UTF-16 code unit offsets inside ONE block's `text`**, exactly as the
  API returns it: diacritics, tatweel, spaces and the block's own `"\n"` all count, one unit each.
  JavaScript string indexes are UTF-16, so use `text.slice(start, end)` directly. **No
  normalisation** (do not strip diacritics, do not use `normalize()`, do not use code points or
  grapheme clusters) before counting. `end` is exclusive.
- Offsets are never counted across blocks. If you render several blocks in one container and the
  browser gives you a selection range over the whole container, convert it to per-block offsets.
- Only `text` and `footnotes` blocks can be highlighted or marked. A heading is not selectable.
  A normal drag selection never crosses from body text into a footnote or across a page.
- Out-of-range values are clamped to the text length when drawing; a span whose `end <= start` after
  clamping is skipped. Never crash on bad offsets: a book's text can change after a mark was made.
- Worked example. Block `p-000052-b-001`, text `الْعِلْمُ نُورٌ يَقْذِفُهُ اللَّهُ فِي قَلْبِ مَنْ يَشَاءُ`.
  The user selects «نُورٌ»: the text before it (`الْعِلْمُ ` = 10 units) gives `start = 10`, `end = 15`,
  and `text.slice(10, 15) === "نُورٌ"`.

**How to do this in the browser.** Render each block as its own element with `data-paragraph-id`,
and keep its text as a single text node of the original string (no extra characters). Draw
highlights and washes as separate overlay elements (CSS Custom Highlight API, or absolutely
positioned boxes from `Range.getClientRects()`), not by splitting the text node, so offsets stay
valid. To convert a selection: for each block the selection touches, compute the start and end
offset inside that block's text node (clip the first and last block to the selection ends, use
`0` and `text.length` for the blocks in between), in reading order. Those are the pieces.

**Pieces for a selection across blocks:** one piece per block touched, in reading order. On a page
the app only allows this inside one "run" of consecutive text blocks (a heading or a footnote block
ends a run). Selecting from a block on one page to a block on another page is possible on the phone
(through the «بداية التحديد» / «نهاية التحديد» menu items) and gives pieces on several pages. The
website may leave that out at first, but must display such records correctly.

---

## 3. Highlights

### 3.1 The record

`kind: "highlight"`, one record per piece; the key is a new upper-case UUID.

```json
{"bookId":"4407","paragraphId":"p-000052-b-001","start":10,"end":15,"color":"yellow",
 "quotedText":"نُورٌ","contentVersion":3,"createdAt":"2026-10-22T14:00:02.390Z"}
```

plus `sessionId` when made inside a session. `updatedAt` = `createdAt` = creation time: a highlight is
**never edited in place**.

### 3.2 Colours (the same in light and dark themes; they are washes over the page)

| `color` | Menu name | Swatch (menus, lists) | Wash on the text |
|---|---|---|---|
| `yellow` | أصفر | `#FFCC00` | the swatch at alpha 0.45 |
| `green` | أخضر | `#33C759` | alpha 0.35 |
| `blue` | أزرق | `#0A85FF` | alpha 0.35 |
| `pink` | وردي | `#FF2E54` | alpha 0.35 |
| `purple` | بنفسجي | `#B052DE` | alpha 0.35 |

### 3.3 Creating, changing, removing (the algorithm)

Highlights **never overlap** in a paragraph. Make a change per touched paragraph:

1. Take the paragraph's existing highlights of the same book and scope (same `bookId`, same
   `sessionId` or none).
2. Cut the selected interval out of them. The parts left outside the selection stay, with their
   colour. For «تظليل» + colour, add the selected interval with the new colour (this **replaces** what
   marked that text before: there is no overlap). For «إزالة التظليل», add nothing.
3. Compare the resulting set with the old rows. A row whose `start`, `end` and `color` are unchanged
   **keeps its record and key**. Every other old row is **deleted** (a tombstone: `deleted: true`,
   `updatedAt = now`), and a **new record** (new key, `createdAt = updatedAt = now`) is created for each
   remaining or new interval.
4. Do not merge neighbouring intervals of the same colour yourself (the phone's behaviour for that is
   not documented; if it merges, it appears as a delete plus a new record on the next sync, which is
   harmless).

So changing a colour gives a new key, and a highlight only partly covered by a removal leaves a
new record for its remainder.

### 3.4 Screens

«كل التظليلات» (inside a session «التظليلات»): grouped by book; each row shows the text, the page
«ص N» and the colour; search («ابحث في التظليلات»); select several and copy («تحديد», «نسخ»); swipe or
button «حذف»; tapping opens the reader at the paragraph with the text marked. The pieces of one
selection (same time, adjacent paragraphs) are grouped into one row. A highlight cannot take a note.

---

## 4. Numbered marks

### 4.1 The record and what its fields mean

```json
{"bookId":"4407","paragraphId":"p-000052-b-002","offset":18,"number":2,
 "noteId":"8A3F0C52-6B1E-4D2A-9C77-2E5B1F4A0D11",
 "spans":[{"paragraphId":"p-000052-b-001","start":16,"end":58},
          {"paragraphId":"p-000052-b-002","start":0,"end":18}],
 "quotedText":"…","contentVersion":3,"createdAt":"2026-10-22T14:05:40.011Z"}
```

plus `sessionId` in a session. One record per mark (not per piece).

| Field | Meaning |
|---|---|
| `paragraphId` | the **last** paragraph of the selection |
| `offset` | UTF-16 offset in **that paragraph's** text where the selection **ended** (`last.end`): where the badge goes |
| `number` | the mark's number; equals the `N` of the line `N. …` in the note |
| `spans` | the marked text, one `{paragraphId, start, end}` per paragraph touched. More than one when the selection crosses paragraphs. May be `[]` on old marks |
| `quotedText` | the text the spans cover, joined with **one space**; may be `""` |
| `contentVersion` | the book's `contentVersion` when it was made (may be absent) |
| `noteId` | the note that holds the numbered line |
| `updatedAt` | creation time, or the last renumber (section 4.5) |

### 4.2 Drawing (to look the same as the phone)

- **Badge:** a pill 18 px high, fully rounded ends (radius 9), width `min(20, max(18, digits × 7 + 8))`,
  background `#A6450F`, number in **Latin digits** (`String(number)`), white, bold, 11 px. It sits in a
  22 px gutter that the reader reserves at the **left edge of the text block**, 1 px from the edge,
  vertically centred on the line where the caret at `offset` of the last paragraph falls
  (`y = caretRect.centerY − 9`). In lists the number is a capsule of the same colour.
- **Wash:** the spans are painted with `rgba(153,153,153,0.30)` per paragraph by `start`/`end`
  (clamped), drawn on top of any highlight. The wash belongs to the mark, not to a highlight.
- A mark is placed by `paragraphId` + `offset` (the badge); its spans are painted in whichever
  paragraphs they name.
- Show a mark only if its note exists and is not deleted (4.7).

### 4.3 Creating a numbered mark

The user selects text → «إضافة رقم» → a sheet «أين تضيف الملاحظة؟» with three choices:

- **«ملاحظة الكتاب»** (subtitle: the book's title): the note that the **most recently created mark of
  this same book and this same scope** points to, if that note still exists; otherwise a **new note**.
- **«ملاحظة جديدة»**: always a new note.
- **«ملاحظاتي»** list: an existing note of the **same place** (the library's notes, or that session's).

A **new note** made here has `title` = the book's title, `hasCustomTitle: true`, `bodyText: ""`,
`sessionId` = the session or absent, `createdAt = updatedAt = now`, no `sortIndex`, no folder.

Then, in this order:

1. **Number:** `max(highest number among the marks whose noteId is this note, highest N of any line
   of the note's full text that matches /^(\d+)\./ (ASCII digits followed by a period, at line start))
   + 1`. Marks of different books may share one note and one numbering.
2. **Append the line** to the note's `bodyText`: `separator + N + ". "` where `separator` is `""` if
   `bodyText` is empty or already ends with `"\n"`, otherwise a single `"\n"`. The number is in ASCII
   digits, followed by a period and **one space**, and nothing else (no quote, no book reference). Set
   `note.updatedAt = now`. After three marks: `"1. شرح الحديث\n2. \n3. "`.
3. **Create the mark record** (fields above). `updatedAt = createdAt = now`.
4. Open the note editor with the cursor at the end of the new line and the keyboard up.

(For a note without a custom title the line is still appended to `bodyText`, which is the end of its
text as well.)

### 4.4 Tapping

- **A badge in the reader** → opens the note (`noteId`) and scrolls to the line that starts with
  `"N. "` (the number, a period, a **space**), cursor at its end, no keyboard. If no such line exists,
  nothing scrolls. (Fields used: `noteId`, `number`.)
- **A numbered line in a note** (a line matching `^(\d+)\.` whose number belongs to a mark of this
  note) → drawn bold in the accent colour `#A6440F` and tappable; tapping opens the book's reader at
  that mark's `paragraphId` with its `spans` highlighted. (Fields used: `noteId`, `number` to find the
  row, then `bookId`, `sessionId`, `paragraphId`, `spans`; `offset` is NOT used.) Only Latin digits
  count: a line starting with Arabic-Indic digits is not a numbered line.
- **Long-press a badge** → menu «حذف الرقم فقط» / «حذف الرقم وسطر الملاحظة».

### 4.5 Removing a mark, and renumbering (the only time numbers change)

When the user removes mark `m` (note `n`):

1. Delete `m` (tombstone, `updatedAt = now`).
2. In the note's text, find the line starting with `m.number + "."` and:
   - **«حذف الرقم وسطر الملاحظة»** (and the swipe «حذف» in the marks list): delete the whole line.
   - **«حذف الرقم فقط»**: cut only the prefix `"N. "` and keep the comment as an ordinary line; if the
     comment is empty, remove the line anyway.
3. Every **later mark of the same note** (higher `number`) moves down by one: `number -= 1`,
   `updatedAt = now`. And every line of the note starting with `M.` where `M` is one of those numbers
   (before the shift) is rewritten to `(M-1).` (rest of the line untouched; apply the mapping to all
   lines at once so a line is never shifted twice).
4. Write the note's text back through the note's `setFullText` rule (section 5.1), so
   **`note.updatedAt = now`**.

A removal therefore changes: the deleted mark, the later marks (`number`, `updatedAt`) and the note
(text, `updatedAt`). Push all of them together in one request.

**What does NOT renumber (known gap, do not "fix" it differently from the phone):** deleting or
reordering lines by hand in the note editor. Nothing watches the text: a mark keeps its number and its
badge, even if its line no longer exists (tapping the badge then opens the note and does not scroll).

### 4.6 Duplicates

The app's own flows never give two marks of one note the same number. It can still happen when two
devices add marks to the same note at the same time (each picks "highest + 1"). Nothing repairs it:
the lookup by (`noteId`, `number`) takes the first row it finds. The website should do the same,
and must not crash or drop one of them.

### 4.7 Deleting notes, orphans

- **A note is deleted** → all marks with that `noteId` are deleted too (a tombstone each). Highlights
  are untouched.
- A mark whose note does not exist is not shown. The phone also deletes it (with a tombstone) the next
  time that book's marks are loaded. The website should hide it, and may tombstone it, but **only after
  a pull has ended with `hasMore: false`** (while pulling, a mark can arrive before its note).

### 4.8 Marks when a book's text changes

The phone does **not** re-find marks: it keeps the old `paragraphId` and offsets (drawn at the same
offsets if the paragraph still exists, so they can drift; not drawn if the paragraph id no longer
exists). It only records `quotedText` and `contentVersion`. Do the same. An improvement for later,
coordinated with the phone: when `contentVersion` differs, look for `quotedText` (folded) in the
paragraph nearest to `start`, then in the neighbouring paragraphs, else keep the old offsets.

---

## 5. Notes

### 5.1 Title and text: ONE continuous document (read this carefully)

A note has `title`, `bodyText`, `hasCustomTitle`. The editor shows **one text with no separate title
field**. What the user sees and types is:

```
fullText(note)  = (hasCustomTitle || title == "") ? bodyText : title + "\n" + bodyText
```

Saving an edited text back (`setFullText(note, text)`), then `note.updatedAt = now`:

- if `hasCustomTitle`: `bodyText = text` (the title is untouched);
- else: split at the **first `"\n"`**: `title` = everything before it (not trimmed, not shortened),
  `bodyText` = everything after it; if there is no newline, `title = text` and `bodyText = ""`.
  So for a note without a custom title **the first line is the title and is not inside `bodyText`**.

A note made by quick-add starts with `title = ""` and the whole text in `bodyText` until its first edit.
So never read `bodyText` alone to show a note: always use `fullText`.

**Displayed title** (lists, headings): `title` trimmed; if empty, the first line of `bodyText` trimmed;
if still empty: «ملاحظة بدون عنوان». Lists show one line, truncated.

**Rename** (list menu «إعادة تسمية» or the editor title; sheet field «العنوان», hint «اتركه فارغًا لاستخدام
السطر الأول من الملاحظة عنوانًا.»), with `name` trimmed:

- non-empty: `title = name`, `hasCustomTitle = true`, `bodyText = fullText` (taken **before** the change,
  so the old first line goes back into the body);
- empty: `hasCustomTitle = false`, then `setFullText(fullText)` (re-split the first line into the title).

`hasCustomTitle` is also `true` for notes made by the numbered-mark flow (title = the book's title).
`title` may be `""` when `hasCustomTitle` is false.

### 5.2 Body

Plain text only: no bold, no lists, no links, no images. Lines separated by `"\n"`. No maximum length
is enforced. Normal content: free text; numbered lines `N. …` (4.3); quotes from the reader:

```
<selected text>
‏——————————
— <author> (ت <year>هـ)، <title>، ج <volume>، ص <page>، <heading>.
```

i.e. the quote, then a divider line (U+200F followed by **10** em dashes `—`), then a citation line that
starts with `"— "`. Pieces of the citation are joined with `"، "`, end with a period, and author,
death year, volume and heading are left out when unknown. The quote is appended to a note as
`bodyText + "\n\n" + quote`, or becomes the whole body of a new note. The phone draws the citation line
(a line starting with an optional U+200F and `—`) smaller and lighter: do the same. A website feature
«add selection to a note» is optional for the first release; if you build it, use exactly this format.
`NoteBookLink` and `NoteExcerpt` are retired and never synced.

For the editor: numbered lines must be styled (bold, `#A6440F`) and tappable (4.4), so a plain
`<textarea>` is not enough for the view mode. Use a read view that renders lines (with the tappable
numbers) and switch to an editing textarea, or an editor library with line decorations.

### 5.3 Folders

One level only: a folder holds notes, folders do not hold folders. A note is in at most one folder
(`folderId`, absent for none). A folder belongs to the library or to one session (`sessionId`).
Screens: «المجلدات» (the folder list, with «كل الملاحظات» first), then a folder's notes.

- Folder `sortIndex` is always present (a number), ascending. A new folder gets `max(sortIndex) + 1`
  (the first gets `0`). Rename: `title`, `updatedAt = now`.
- **Deleting a folder:** its notes stay (`folderId` removed, `orderChangedAt = now`), the folder gets a
  tombstone.

### 5.4 Order in lists (`sortIndex`, `orderChangedAt`)

Notes and sessions: items with **no** `sortIndex` come first, among themselves newest first (notes by
`updatedAt`, sessions by `createdAt`); then the items with a number, ascending. So a new note shows at
the top.

**Reordering by drag** (not while a search is active): renumber the **whole displayed list** by
position, `sortIndex = 0, 1, 2 …` (whole numbers), and set `orderChangedAt = now` on **every** item of
the list. `updatedAt` is NOT changed (order is merged separately from content, brief 1 section 2.4).

**Moving a note to a folder** (swipe «نقل إلى مجلد», or «نقل» for several; «بدون مجلد» clears it; only
folders of the same place are offered): set `folderId` (or remove it), **remove `sortIndex`** (the note
goes to the top of the new folder), `orderChangedAt = now`, and **push it with
`updatedAt = max(updatedAt, orderChangedAt)`**. This is the one place where the wire `updatedAt` is not
the text-edit time, and it is needed: `folderId` is content, and the server only takes content from the
newer `updatedAt`. Do the same when a folder deletion removes `folderId` from its notes.

### 5.5 Notes and sessions

A note's `sessionId` is fixed when it is made (the session you were in), and it is shown only in that
session's «الملاحظات». A library note appears only in the main «الملاحظات». There is no screen to move
a note between a session and the library, or a folder from one session to another: a move to a folder
offers only the folders of the same place. (Do not build one without agreeing it with the phone.)

### 5.6 «(نسخة محفوظة)» copies

The **server** makes them when two devices edit the same note at the same time (brief 1, 2.4):
the loser's text becomes a separate note titled `<title> (نسخة محفوظة)` with `copyOf`. The phone does
not read `copyOf` or treat them specially: they are ordinary notes, openable, editable, renameable,
movable and deletable. There is no merge tool (merging is copy and paste). Show them like any note.

### 5.7 Search, selection, copy

- **Search** («ابحث في هذا المجلد»): matches title and text after folding both sides, and shows the first
  matching line in the row. Use the same folding as the server's `normalize()` (file
  `app/services/arabic.py`, also described in brief 1): NFC; remove tashkeel (U+064B–U+0670,
  U+06D6–U+06ED), tatweel (U+0640) and invisible marks (U+200B–U+200F, U+202A–U+202E,
  U+2066–U+2069, U+FEFF); fold letters `أ إ آ ٱ → ا`, `ى → ي`, `ة → ه`, Persian `ک → ك`, `ی → ي`,
  `ھ ۀ → ه`, `گ → ك`, `پ → ب`, `چ → ج`, `ژ → ز`; Arabic-Indic and Persian digits → ASCII; lowercase;
  collapse whitespace.
- **Selection mode** («اختر ملاحظات»): move, copy, delete several. **«نسخ»** copies the selected notes as one
  text in list order: each note's title, a blank line, its text; notes separated by
  `"\n\n" + 12 em dashes + "\n\n"`. There is no share sheet and no file export.
- **Deleting notes** (swipe, or several, with the confirmation «حذف N ملاحظة؟»): also deletes their
  numbered marks (4.7).
- No duplicate, pin, tags or colours.

---

## 6. Study sessions («جلسة علم»): the Sessions tab

### 6.1 Screens and labels

- **List** «جلسة علم»: rows show the title and «N كتب»; a `+` («جلسة جديدة») opens a field (placeholder
  «مثلًا: بحث في الإمامة», buttons «إنشاء» / «إلغاء»); an empty title becomes «جلسة جديدة». Swipe or
  long-press: «إعادة تسمية», «حذف» (confirmation «حذف الجلسة», message «يُحذف تقدّم القراءة والإشارات
  والتظليلات والملاحظات الخاصة بهذه الجلسة. لا تُحذف الكتب المحملة.»); drag to reorder.
- **One session:** empty state «اجمع كتبك .. وواصل حيث توقفت» / «الكتب المضافة إلى جلستك العلمية»; four
  tool tiles «الملاحظات», «التظليلات», «الإشارات المرقّمة», «الإشارات المرجعية»; «كتب الجلسة» (the books,
  two columns); button «إضافة كتب إلى الجلسة» («ابحث عن كتاب وأضفه إلى جلستك العلمية»); a search button
  «بحث» (searches only the session's books); «فهرس الجلسة» (the contents of the session's books);
  selection mode «تحديد» / «تحديد الكل» / «إلغاء» with the bar «اختر كتبًا لإزالتها من الجلسة» and the
  buttons «تحميل الكل», «إزالة»; per book a menu «معلومات الكتاب», «إزالة من الجلسة»; a book's line
  shows «الصفحة N» (the session's own position) or «لم تبدأ بعد».
- **Adding books:** from the session screen, and from any book's own sheet «إضافة إلى جلسة» (ticks the
  sessions that hold the book; a new-session row creates one that holds it). When a book is added from
  the reader's sheet, the session's position for it is seeded with the reader's current position,
  unless the session already has one.
- **Reorder books:** drag a book onto another; the dragged book is removed and inserted at the target's
  index. **Rename:** `title`, `updatedAt = now`, never empty.
- (The «تحميل الكل» download button is device-specific and not synced; the website can leave it out.)

### 6.2 The data

- `session`: `title`, `createdAt`, optional `sortIndex`; **no book list**. Its `orderChangedAt` follows
  the user's reordering (5.4).
- `sessionBook`: one record per book, key `{sessionId}|{bookId}`, `data {sessionId, bookId, position}`,
  **always with `orderChangedAt`**. **Sort by `position` ascending and never assume the values are
  contiguous** (the phone writes 0, 1, 2 … but other values exist in the data, and the phone
  renumbers a gappy list on its next push without changing the order).
  - *Add* a book: new record, `position` = the old count, `updatedAt = orderChangedAt = now`.
  - *Remove* a book: push it with `deleted: true`; the books after it move up one index, so push each of
    those again with its new `position` and `orderChangedAt = now`.
  - *Move* a book: push again every book whose index changed, with the new `position` and
    `orderChangedAt = now`; an existing record keeps its `updatedAt`.
  - (Only reordering touches `position` and `orderChangedAt`; they are the order part of the merge.)
- **Reading position.** Reading a book **inside a session** writes only `sessionProgress` (key
  `{sessionId}|{bookId}`); from the library only `readingProgress` (key `{bookId}`). They never write
  each other. Both have `lastLocationId` (the page's first paragraph id), `progressPercent`
  (0 to 1, `(page index + 1) / page count`), optional `pageScrollFraction` (0 top to 1 bottom),
  `lastReadAt` (also the record's `updatedAt`). Save when the page changes or the reader closes.
  **Continue reading:** a book opened in a session resumes **that session's** position (or the start
  if none); opened from the library, the library's. «قيد المطالعة» on the library home lists the
  library's `readingProgress` by `lastReadAt` newest first and ignores session positions.

### 6.3 Deleting a session (what the website must push)

The server does not cascade deletes. On confirmation, push tombstones (`deleted: true`,
`updatedAt = now`) for the session AND everything that carries its `sessionId`: its `sessionBook`s,
`sessionProgress` rows, notes, **note folders**, highlights, numbered marks and bookmarks. (The phone
currently forgets the note folders, a known bug that leaves invisible folders behind; the website
should include them, and it is harmless to the phone.) Downloaded books are not affected.

---

## 7. Sync rules specific to these kinds

- **Absent, not null.** In `data`, a key with no value is **left out** (not `null`): `sessionId`, `folderId`,
  `sortIndex`, `quotedText`, `contentVersion`, `pageScrollFraction`. Read a missing key as "none" and
  also accept `null`. Write the same way (omit empty keys). Keep unknown extra fields you did not write.
- **No extra fields:** the phone writes only the fields in brief 1, section 2.3. It never writes or reads
  `copyOf`.
- **`updatedAt` means** (the wire value): note: the last edit of the text/title (a move to a folder:
  `max(updatedAt, orderChangedAt)`, 5.4); folder: last rename; session: last rename; sessionBook:
  when first added (kept); highlight: creation; numbered mark: creation or last renumber; bookmark:
  creation; progress: `lastReadAt`. Never earlier than 2020-01-01.
- **`orderChangedAt` is sent only for** `note`, `noteFolder`, `session` (from reordering or a folder
  move) and always for `sessionBook`. Highlights, marks, bookmarks and progress never send it.
- **`baseSeq`.** Brief 1 says to send the `seq` the record had when you last pulled it (each pulled item
  carries `seq`). The phone sends the cursor of its last pull, which works the same way. The one case
  that needs care: **if a pull brings a record that you have also changed locally (not pushed yet),
  keep your local version, do NOT update its `seq`, and push it with the OLD baseSeq** (the one from
  before this pull). The server sees that the stored version is newer than your base, keeps the winner
  by time, and for **notes** saves the loser's text as a «(نسخة محفوظة)» copy. If you updated `seq`
  from the pull, the conflict would be invisible and the other device's text would be silently lost.
- **Keys** compare case-insensitively; ids are upper-case UUIDs.
- **Marks and notes arrive separately.** While a pull has `hasMore: true` a mark can come before its
  note: decide about orphans only after the pull is complete.
- **Push a related change together:** a removed mark, its renumbered siblings and the edited note go
  in one request; a deleted session and everything in it go in one request (up to 500 items; split
  larger ones, deletes of children first or together, never leaving a half-deleted state for long).

---

## 8. Things the iPhone app does not define (do not invent them)

- **Bookmarks** («الإشارات المرجعية», kind `bookmark`, key `{sessionId or "-"}|{bookId}|{locationId}`, data
  `bookId`, `sessionId?`, `locationId`, `createdAt`): the phone's document does not describe their
  screens or how they are created and removed. Ask the iPhone developer before building them. Until
  then, show bookmarks read-only if you show them at all.
- Whether the phone merges neighbouring highlights of the same colour (3.3).
- Re-finding marks after a book's text changes (4.8): the phone does not do it.
- Whether a pure reorder also pushes the bumped `updatedAt` for notes (5.4 says: only for folder moves).

---

## 9. Examples (hand-built from the app's mapping code; ids and times are made up)

Wrapper of a pushed record (`orderChangedAt` only for note, noteFolder, session, sessionBook):

```json
{"kind":"note","key":"…","deleted":false,"updatedAt":"…","baseSeq":41,"data":{…}}
```

A deletion is `{"kind":"…","key":"…","deleted":true,"updatedAt":"…","baseSeq":41}` with no `data`.

**A note with numbered lines** (created by the numbered-mark flow: custom title; two marks so far):

```json
{"kind":"note","key":"8A3F0C52-6B1E-4D2A-9C77-2E5B1F4A0D11","deleted":false,
 "updatedAt":"2026-10-22T14:03:11.482Z","baseSeq":41,
 "data":{"title":"الكافي","bodyText":"1. شرح الحديث الأول\n2. ","hasCustomTitle":true,
         "createdAt":"2026-10-22T14:00:02.117Z"}}
```

**A numbered mark with one span** (the worked example of section 2, `number` 1, in the note above):

```json
{"kind":"numberedMark","key":"C91D4E7A-0B3F-4A58-8E21-7F6A2B9C5D30","deleted":false,
 "updatedAt":"2026-10-22T14:00:02.390Z","baseSeq":41,
 "data":{"bookId":"4407","paragraphId":"p-000052-b-001","offset":15,"number":1,
         "noteId":"8A3F0C52-6B1E-4D2A-9C77-2E5B1F4A0D11",
         "spans":[{"paragraphId":"p-000052-b-001","start":10,"end":15}],
         "quotedText":"نُورٌ","contentVersion":3,"createdAt":"2026-10-22T14:00:02.390Z"}}
```

**A numbered mark with two spans, made inside a session** (selection from inside `…b-001` to inside
`…b-002`; `paragraphId` and `offset` are the last span's; `quotedText` joins the spans with one space):

```json
{"kind":"numberedMark","key":"1F2E3D4C-5B6A-4798-8A9B-0C1D2E3F4A5B","deleted":false,
 "updatedAt":"2026-10-22T14:05:40.011Z","baseSeq":41,
 "data":{"bookId":"4407","sessionId":"5D0A7B16-93C8-4E0F-A2D4-6B1C8E9F3A72",
         "paragraphId":"p-000052-b-002","offset":18,"number":2,
         "noteId":"8A3F0C52-6B1E-4D2A-9C77-2E5B1F4A0D11",
         "spans":[{"paragraphId":"p-000052-b-001","start":16,"end":58},
                  {"paragraphId":"p-000052-b-002","start":0,"end":18}],
         "quotedText":"يَقْذِفُهُ اللَّهُ فِي قَلْبِ مَنْ يَشَاءُ وَلَيْسَ الْعِلْمُ",
         "contentVersion":3,"createdAt":"2026-10-22T14:05:40.011Z"}}
```

(In real data the note a session mark points to also has that `sessionId`.)

**Highlights** (`data` only; wrapper as above; offsets in the second to fifth lines are illustrative):

```json
{"bookId":"4407","paragraphId":"p-000052-b-001","start":10,"end":15,"color":"yellow","quotedText":"نُورٌ","contentVersion":3,"createdAt":"2026-10-22T14:00:02.390Z"}
{"bookId":"4407","paragraphId":"p-000052-b-001","start":0,"end":8,"color":"green","quotedText":"الْعِلْمُ","contentVersion":3,"createdAt":"2026-10-22T14:01:10.204Z"}
{"bookId":"4407","sessionId":"5D0A7B16-93C8-4E0F-A2D4-6B1C8E9F3A72","paragraphId":"p-000052-b-002","start":6,"end":18,"color":"purple","quotedText":"الْعِلْمُ","contentVersion":3,"createdAt":"2026-10-22T14:02:44.093Z"}
```

**A folder** (`sortIndex` always present; `orderChangedAt` only after a drag):

```json
{"kind":"noteFolder","key":"E4B72A90-1C5D-4F38-B6A1-9D0E3C7F2A84","deleted":false,
 "updatedAt":"2026-10-22T13:58:00.000Z","orderChangedAt":"2026-10-22T14:10:00.000Z","baseSeq":41,
 "data":{"title":"أبحاث الإمامة","sortIndex":0,"createdAt":"2026-10-22T13:58:00.000Z"}}
```

**A session with books** (no book list on the session; one record per book):

```json
{"kind":"session","key":"5D0A7B16-93C8-4E0F-A2D4-6B1C8E9F3A72","deleted":false,
 "updatedAt":"2026-10-22T13:40:00.000Z","baseSeq":41,
 "data":{"title":"بحث في الإمامة","createdAt":"2026-10-22T13:40:00.000Z"}}
{"kind":"sessionBook","key":"5D0A7B16-93C8-4E0F-A2D4-6B1C8E9F3A72|4407","deleted":false,
 "updatedAt":"2026-10-22T13:45:00.000Z","orderChangedAt":"2026-10-22T13:45:00.000Z","baseSeq":41,
 "data":{"sessionId":"5D0A7B16-93C8-4E0F-A2D4-6B1C8E9F3A72","bookId":"4407","position":0}}
```

**A session reading position** (`readingProgress` has the same fields without `sessionId`, key = `bookId`):

```json
{"kind":"sessionProgress","key":"5D0A7B16-93C8-4E0F-A2D4-6B1C8E9F3A72|4407","deleted":false,
 "updatedAt":"2026-10-22T14:07:00.000Z","baseSeq":41,
 "data":{"sessionId":"5D0A7B16-93C8-4E0F-A2D4-6B1C8E9F3A72","bookId":"4407",
         "lastLocationId":"p-000052-b-001","progressPercent":0.18,
         "lastReadAt":"2026-10-22T14:07:00.000Z","pageScrollFraction":0.35}}
```

---

## 10. Test checklist (with a phone signed into the same account)

- Notes made on the phone (with custom title, without, with numbered lines, in a folder, in a session)
  show correctly on the website: titles, `fullText`, folder order, newest-first for unarranged notes.
- A highlight of each colour made on the phone lands on exactly the same words on the website
  (diacritics included), and the reverse.
- A numbered mark with two spans: the badge sits in the gutter at the line where the last span ends;
  the wash covers both spans; tapping the badge scrolls the note to `N. `; tapping `N.` in the note
  opens the reader at the mark.
- Add three marks on the website: the note reads `1. …\n2. \n3. ` with a correct separator, and the
  numbers continue after any hand-typed `N.` lines. Remove mark 2 on the website: mark 3 becomes 2 and
  its line is renumbered, the note's `updatedAt` changes, and the phone shows the same after sync.
- Edit the same note on both devices offline, then sync both: the newer text wins and the older
  appears as a «(نسخة محفوظة)» note; nothing is lost.
- Reorder notes on one device and edit one on the other: both survive. Move a note to a folder on the
  website: the phone shows it in that folder at the top.
- Delete a note: its marks disappear on both. Delete a session: the session, its books, positions,
  notes, folders, highlights, marks and bookmarks are gone on both, and none of it shows in the library.
- Add and reorder books in a session on each device: the order agrees, with gaps in `position` tolerated.
- Open a book inside a session and from the library: the two reading positions stay separate.
