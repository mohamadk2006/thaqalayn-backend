"""Fold Persian gaf, pe, che, zhe into Arabic kaf, ba, jim, zay for search.

Arabic spells Persian names and words without these letters ("الكلبايكاني" for
"الگلپايگاني"), so the same name is in the library both ways, and the iPhone app's own
search already folds them. arabic_normalize() gets the four pairs -- a snapshot of
database/sql/arabic_normalize.sql as of this revision, mirrored by app/services/arabic.py.

The stored normalized copies are refolded here (authors, works, books, sections,
collection names -- about 322k rows, none of which collide under a unique constraint on
production). The catalog-change triggers watch the displayed title and name, not these
columns, so no catalog change reaches the apps.

Pages are not touched here: 1.6 million of production's 7.9 million page indexes hold one
of the letters, which would hold the deploy for an hour. persian_fold_tsvector() rewrites
one page's stored index in place -- the same lexemes, positions kept, the four letters
folded -- and scripts/search_index/fold_persian_letters.py runs it over the pages in
batches afterwards (no book files needed: the index already holds the normalized words).

Revision ID: 5e7a2c9d41b3
Revises: f41d0c7a9b22
"""
from collections.abc import Sequence

from alembic import op

revision: str = "5e7a2c9d41b3"
down_revision: str | Sequence[str] | None = "f41d0c7a9b22"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERSIAN = r"\06AF\067E\0686\0698"  # گ پ چ ژ, as U&'' escapes
ARABIC = r"\0643\0628\062C\0632"   # ك ب ج ز

NORMALIZED_COLUMNS = [
    ("authors", "name_norm"),
    ("works", "title_norm"),
    ("books", "title_norm"),
    ("sections", "title_norm"),
    ("shamela_collections", "normalized"),
]


def upgrade() -> None:
    # One command per execute(): asyncpg cannot prepare several at once.
    op.execute(NORMALIZE_FUNCTION_SQL)
    op.execute(NORMALIZE_FUNCTION_COMMENT_SQL)
    op.execute(FOLD_TSVECTOR_SQL)
    for table, column in NORMALIZED_COLUMNS:
        op.execute(
            f"UPDATE {table} SET {column} = translate({column}, U&'{PERSIAN}', U&'{ARABIC}') "
            f"WHERE {column} ~ U&'[{PERSIAN}]'"
        )


def downgrade() -> None:
    # The function goes back; refolded titles and page indexes stay folded (they still
    # match everything they matched, plus the Arabic spellings).
    op.execute(PREVIOUS_FUNCTION_SQL)
    op.execute("DROP FUNCTION IF EXISTS persian_fold_tsvector(tsvector);")


NORMALIZE_FUNCTION_SQL = r"""
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
                        normalize(input, NFC),
                        U&'[\064B-\065F\0670\06D6-\06ED\0640\200B-\200F\202A-\202E\2066-\2069\FEFF]',
                        '',
                        'g'
                    ),
                    --   أ إ آ ٱ → ا      ى → ي      ة → ه
                    --   ک → ك   ی → ي   ھ ۀ → ه     گ → ك   پ → ب   چ → ج   ژ → ز
                    --   ٠-٩ and ۰-۹ → 0-9
                    U&'\0623\0625\0622\0671\0649\0629\06A9\06CC\06BE\06C0\06AF\067E\0686\0698\0660\0661\0662\0663\0664\0665\0666\0667\0668\0669\06F0\06F1\06F2\06F3\06F4\06F5\06F6\06F7\06F8\06F9',
                    U&'\0627\0627\0627\0627\064A\0647\0643\064A\0647\0647\0643\0628\062C\0632\0030\0031\0032\0033\0034\0035\0036\0037\0038\0039\0030\0031\0032\0033\0034\0035\0036\0037\0038\0039'
                )
            ),
            U&'[\0009-\000D\0020\0085\00A0\1680\2000-\200A\2028\2029\202F\205F\3000]+',
            ' ',
            'g'
        )
    );
$$;
"""

NORMALIZE_FUNCTION_COMMENT_SQL = r"""
COMMENT ON FUNCTION arabic_normalize(text) IS
    'Folds Arabic text to its canonical searchable form: NFC, strip tashkeel/tatweel/'
    'invisibles, fold alef+ya+ta-marbuta+Persian letters (incl. گ پ چ ژ), fold Arabic-Indic digits, '
    'lowercase, collapse whitespace. Mirrored byte-for-byte by app/services/arabic.py. '
    'For matching only — never modifies stored or displayed text.';
"""

FOLD_TSVECTOR_SQL = r"""
CREATE OR REPLACE FUNCTION persian_fold_tsvector(v tsvector)
RETURNS tsvector
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
RETURNS NULL ON NULL INPUT
AS $$
    -- Each lexeme with the four letters folded, written back in tsvector's own text form
    -- with its positions, so phrase search still works; lexemes that fold to the same
    -- word merge their positions, exactly as to_tsvector() would have made them.
    SELECT coalesce(string_agg(
               '''' || replace(replace(
                   translate(lexeme, U&'\06AF\067E\0686\0698', U&'\0643\0628\062C\0632'),
                   '\', '\\'), '''', '''''') || ''''
               || coalesce(':' || array_to_string(positions, ','), ''),
               ' '), '')::tsvector
    FROM unnest(v)
$$;
"""

PREVIOUS_FUNCTION_SQL = r"""
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
                        normalize(input, NFC),
                        U&'[\064B-\065F\0670\06D6-\06ED\0640\200B-\200F\202A-\202E\2066-\2069\FEFF]',
                        '',
                        'g'
                    ),
                    U&'\0623\0625\0622\0671\0649\0629\06A9\06CC\06BE\06C0\0660\0661\0662\0663\0664\0665\0666\0667\0668\0669\06F0\06F1\06F2\06F3\06F4\06F5\06F6\06F7\06F8\06F9',
                    U&'\0627\0627\0627\0627\064A\0647\0643\064A\0647\0647\0030\0031\0032\0033\0034\0035\0036\0037\0038\0039\0030\0031\0032\0033\0034\0035\0036\0037\0038\0039'
                )
            ),
            U&'[\0009-\000D\0020\0085\00A0\1680\2000-\200A\2028\2029\202F\205F\3000]+',
            ' ',
            'g'
        )
    );
$$;
"""
