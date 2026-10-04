# Entity-relationship diagram (conceptual)

The library's entities and how they relate, without join tables, foreign-key columns or the
technical tables. The physical tables are in the migrations (`database/migrations/versions`).

```mermaid
erDiagram
    AUTHOR ||--o{ WORK : writes
    WORK ||--|{ BOOK : "is published as (volumes)"
    AUTHOR ||--o{ BOOK : "is credited on"
    BOOK ||--|{ PAGE : "is made of"
    BOOK ||--o{ SECTION : "is divided into"
    SECTION ||--o{ PAGE : "spans"
    SUBJECT }o--o{ WORK : classifies
    LIBRARY }o--o{ WORK : "shelves"
    LIBRARY ||--o{ LIBRARY : "contains sub-libraries"
    SUBJECT }o--o{ BOOK : "pins in order"
    LANGUAGE ||--o{ WORK : "is the language of"
    SOURCE_COLLECTION ||--o{ BOOK : "was imported from"

    AUTHOR {
        text name
        text death_date "as written: ت 329 / قرن 3 / معاصر"
        int death_year_hijri
    }
    WORK {
        text title
        int volume_count
        bool is_featured
    }
    BOOK {
        int volume
        text title
        text publisher
        text edition
        text published_year
        text editor
        text isbn
        bool is_published
        int page_count
    }
    PAGE {
        int sequence "position in the book"
        text page_number "printed label, or 0.1 for front matter"
        text page_type
        tsvector search_index "the page text is kept in the book file"
    }
    SECTION {
        text title
        int order
    }
    SUBJECT {
        text title
        int sort_order
    }
    LIBRARY {
        text title
        int sort_order
    }
    LANGUAGE {
        text name
    }
    SOURCE_COLLECTION {
        text name
        int book_count
    }
```

## Reading it

- A **work** is a title by an author; its **books** are its volumes (one book for a single-volume work).
  Pages and sections belong to a book.
- A work can belong to many **subjects** and many **libraries** (a tree of libraries inside libraries);
  each of those can hold many works.
- A subject can pin books in a chosen order.
- Not shown: the technical tables (change log for the apps' catalog sync, import history, schema version).
