# Database ER diagram

PostgreSQL 17, schema `public`, as created by the Alembic migrations (head `5e7a2c9d41b3`).
Render with any Mermaid viewer (GitHub shows it inline).

```mermaid
erDiagram
    languages ||--o{ works : "language_code"
    languages ||--o{ books : "language_code"
    languages ||--o{ shamela_collections : "language_hint"
    authors ||--o{ works : "author_id"
    authors ||--o{ books : "author_id"
    works ||--|{ books : "work_id (volumes)"
    shamela_collections ||--o{ books : "collection_id"
    books ||--o{ pages : "book_id"
    books ||--o{ sections : "book_id"
    sections ||--o{ pages : "section_id"
    works ||--o{ work_subjects : "work_id"
    subjects ||--o{ work_subjects : "subject_id"
    works ||--o{ library_works : "work_id"
    libraries ||--o{ library_works : "library_id"
    libraries ||--o{ libraries : "parent_id"
    subjects ||--o{ subject_pinned_books : "subject_id"
    books ||--o{ subject_pinned_books : "book_id"
    books ||--o{ import_log : "book_id (no FK)"
    books ||--o{ book_changes : "book_id (no FK)"

    languages {
        varchar code PK
        varchar name
    }
    authors {
        int id PK
        text name
        text name_norm "search fold"
        varchar death_label "as written: 'ت 329', 'قرن 3', 'معاصر'"
        int death_year_hijri "a century label is stored as the century number"
    }
    works {
        int id PK
        text title
        text title_norm
        int author_id FK
        varchar language_code FK
        int volume_count
        bigint total_content_bytes
        text grouping_warning
        bool is_featured
        int featured_sort_order
    }
    books {
        int id PK "one volume; book id = work volume"
        int work_id FK
        int volume
        text title
        text title_norm
        int author_id FK
        varchar language_code FK
        int collection_id FK
        text publisher
        text edition
        text published_year
        text printer
        text editor
        text isbn
        text description
        bool is_verified
        bool is_published
        text content_path "books_root/id.json"
        varchar content_sha256
        bigint content_bytes
        bigint download_bytes
        text cover_path
        int content_version
        int page_first
        int page_last
        int page_count
        int paragraph_count
        int section_count
        timestamptz imported_at
        timestamptz created_at
        timestamptz updated_at
    }
    sections {
        bigint id PK
        int book_id FK
        int ord
        text title
        text title_norm
        int page_start_sequence
        int page_end_sequence
    }
    pages {
        bigint id PK
        int book_id FK
        int sequence "position in the book"
        text page_number "label: '12', '0.1' (front matter)"
        text page_type
        bool is_blank
        bigint section_id FK
        jsonb block_offsets
        tsvector search_tsv "to_tsvector('simple', arabic_normalize(text)); the page text itself is not stored"
    }
    subjects {
        text id PK
        text title
        int sort_order
        text section
    }
    work_subjects {
        int work_id PK, FK
        text subject_id PK, FK
    }
    libraries {
        int id PK
        text title
        int parent_id FK
        int sort_order
    }
    library_works {
        int library_id PK, FK
        int work_id PK, FK
    }
    subject_pinned_books {
        text subject_id PK, FK
        int book_id PK, FK
        int position
    }
    shamela_collections {
        int id PK
        text raw
        text normalized
        varchar language_hint FK
        int book_count
    }
    import_log {
        bigint id PK
        varchar run_id
        int book_id
        text source_file
        varchar status
        varchar stage
        text message
        varchar content_sha256
        timestamptz at
    }
    book_changes {
        int book_id PK
        bigint seq "catalog sync cursor"
        xid8 xid
        timestamptz changed_at
    }
    catalog_stamps {
        text scope PK
        text key PK
        bigint seq
        xid8 xid
    }
    catalog_meta {
        text key PK
        bigint value
    }
    alembic_version {
        varchar version_num PK
    }
```

## How it reads

- **Catalog:** a **work** (a title by an author) has one or more **books** (its volumes). Works belong to
  many **subjects** (`work_subjects`) and to many **libraries** (`library_works`, a separate tree through
  `parent_id`). `subject_pinned_books` fixes the order of chosen books inside a subject.
- **Content:** a book's text lives in `books_root/<id>.json`; the database keeps its **pages** (position,
  label, the search index `search_tsv`) and its **sections** (the table of contents), not the text.
- **Search:** `pages.search_tsv` (GIN index). Search orders results by the author's death year
  (`authors.death_year_hijri`, or the middle of the century for a «قرن N» label), then work title, volume, page.
- **Sync for the apps:** `book_changes`, `catalog_stamps` and `catalog_meta` are the change log the iPhone apps
  read to update their catalog; they are written by database triggers, not by the application.
- **Bookkeeping:** `import_log` (one row per import attempt, `book_id` is not a foreign key so a deleted book's
  history stays), `shamela_collections` (the source collection names books were imported under) and
  `alembic_version`.
