# Sample input faces

Three freely-licensed portraits of public figures, used as **stage 1 input** for the
pipeline. They are public figures on purpose: the whole point of stage 2 is that the
search is live, so the input has to be a face the open web can actually find. No
private individuals are used anywhere in this project.

Every file was fetched from **Wikimedia Commons** through the MediaWiki
`action=query&generator=search` API (namespace 6, `iiurlwidth=800`), with a descriptive
`User-Agent` — Commons blocks the default `python-requests` agent. All three verified as
real JPEGs (`FF D8 FF` magic bytes).

| File | Subject | Bytes | Commons page | Author / credit | Licence |
| --- | --- | --- | --- | --- | --- |
| `elon-musk.jpg` | Elon Musk | 242,053 | [File:Elon Musk 2015.jpg](https://commons.wikimedia.org/wiki/File:Elon_Musk_2015.jpg) | Steve Jurvetson ([source](https://www.flickr.com/photos/jurvetson/18659265152/)) | [CC BY 2.0](https://creativecommons.org/licenses/by/2.0) |
| `sundar-pichai.jpg` | Sundar Pichai | 48,449 | [File:Jokowi and Sundar Pichai Googleplex.jpg](https://commons.wikimedia.org/wiki/File:Jokowi_and_Sundar_Pichai_Googleplex.jpg) | Consulate General of Indonesia in San Francisco | Public domain |
| `lionel-messi.jpg` | Lionel Messi | 216,906 | [File:Lionel Messi in 2018.jpg](https://commons.wikimedia.org/wiki/File:Lionel_Messi_in_2018.jpg) | Кирилл Венедиктов ([soccer.ru](https://www.soccer.ru/galery/1055457/photo/733494)) | [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0) |

## Attribution

`elon-musk.jpg` and `lionel-messi.jpg` are CC-BY / CC-BY-SA and **require attribution on
reuse**. The rows above carry the author, the source link and the licence; keep them with
the images if you copy them out of this repository. `sundar-pichai.jpg` is public domain
and carries no attribution requirement, though the credit is recorded anyway.

Wikimedia Commons is one of the search providers in stage 2. That is not a shortcut — the
input file is never passed to the search layer, only its 128-d embedding is, and any
Commons result still has to be downloaded, re-detected, re-encoded and clear the cosine
threshold like every other candidate.

## Usage

```bash
python run_pipeline.py --image samples/elon-musk.jpg --hint "Elon Musk"
```
