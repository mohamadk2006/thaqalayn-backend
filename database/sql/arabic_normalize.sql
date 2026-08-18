-- Arabic normalization for search — canonical source.
--
-- This file is the readable source of truth. Each Alembic revision that changes the
-- function embeds a *snapshot* of it inline, so migration history stays accurate even
-- as this file evolves. When you change this file, generate a new revision; never edit
-- an applied one.
--
-- There is a byte-for-byte identical implementation in Python at app/services/arabic.py.
-- The two MUST agree exactly: the search index is built by this SQL function and queried
-- through the Python one, so a divergence produces silently wrong results rather than an
-- error. tests/test_arabic_parity.py enforces agreement against real corpus text.
--
-- IMMUTABLE is required, not merely desirable: the pages.search_tsv generated column
-- calls this, and PostgreSQL only permits immutable functions there. Every builtin used
-- below (normalize, regexp_replace, translate, lower, btrim) is itself immutable.

CREATE OR REPLACE FUNCTION arabic_normalize(input text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
RETURNS NULL ON NULL INPUT
AS $$
    SELECT btrim(
        regexp_replace(
            lower(
                translate(
                    regexp_replace(
                        -- NFC first: 23.6% of real corpus paragraphs are not composed,
                        -- and a decomposed alef+hamza must become أ before the letter
                        -- folding below can see it.
                        normalize(input, NFC),
                        -- Removed outright: tashkeel (U+064B–U+065F), superscript alef
                        -- (U+0670), Quranic annotation marks (U+06D6–U+06ED), tatweel
                        -- (U+0640), and zero-width/bidi controls + BOM.
                        U&'[\064B-\065F\0670\06D6-\06ED\0640\200B-\200F\202A-\202E\2066-\2069\FEFF]',
                        '',
                        'g'
                    ),
                    -- Folded pairs, in the same order as _LETTER_FOLDING then
                    -- _DIGIT_FOLDING in app/services/arabic.py:
                    --   أ إ آ ٱ → ا      ى → ي      ة → ه
                    --   ک → ك   ی → ي   ھ ۀ → ه     ٠-٩ and ۰-۹ → 0-9
                    -- Adjacent U&'' literals do not concatenate, so each argument is a
                    -- single literal. ASCII digits are escaped too (\0030-\0039) purely
                    -- to keep the two strings visually aligned at 30 characters each.
                    U&'\0623\0625\0622\0671\0649\0629\06A9\06CC\06BE\06C0\0660\0661\0662\0663\0664\0665\0666\0667\0668\0669\06F0\06F1\06F2\06F3\06F4\06F5\06F6\06F7\06F8\06F9',
                    U&'\0627\0627\0627\0627\064A\0647\0643\064A\0647\0647\0030\0031\0032\0033\0034\0035\0036\0037\0038\0039\0030\0031\0032\0033\0034\0035\0036\0037\0038\0039'
                )
            ),
            -- Explicit whitespace class rather than \s: PostgreSQL's \s and Python's are
            -- not the same set (NBSP, in particular), and the two implementations have to
            -- agree exactly.
            U&'[\0009-\000D\0020\0085\00A0\1680\2000-\200A\2028\2029\202F\205F\3000]+',
            ' ',
            'g'
        )
    );
$$;

COMMENT ON FUNCTION arabic_normalize(text) IS
    'Folds Arabic text to its canonical searchable form: NFC, strip tashkeel/tatweel/'
    'invisibles, fold alef+ya+ta-marbuta+Persian letterforms, fold Arabic-Indic digits, '
    'lowercase, collapse whitespace. Mirrored byte-for-byte by app/services/arabic.py. '
    'For matching only — never modifies stored or displayed text.';
