# Sample input faces

Three freely-licensed portraits of public figures, used as **stage 1 input** for the
pipeline. They are public figures on purpose: the whole point of stage 2 is that the
search is live, so the input has to be a face the open web can actually find. No
private individuals are used anywhere in this project.

Every file was fetched from **Wikimedia Commons** through the MediaWiki
`action=query` API (namespace 6, `iiprop=url|extmetadata`), with a descriptive
`User-Agent` — Commons blocks the default `python-requests` agent. All three verified as
real JPEGs (`FF D8 FF` magic bytes).

| File | Subject | Bytes | Commons page | Author / credit | Licence |
| --- | --- | --- | --- | --- | --- |
| `elon-musk.jpg` | Elon Musk | 242,053 | [File:Elon Musk 2015.jpg](https://commons.wikimedia.org/wiki/File:Elon_Musk_2015.jpg) | Steve Jurvetson ([source](https://www.flickr.com/photos/jurvetson/18659265152/)) | [CC BY 2.0](https://creativecommons.org/licenses/by/2.0) |
| `sundar-pichai.jpg` | Sundar Pichai | 527,485 | [File:Sundar Pichai - 2023 (cropped).jpg](https://commons.wikimedia.org/wiki/File:Sundar_Pichai_-_2023_(cropped).jpg) | Lukasz Kobus (European Commission) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0) |
| `lionel-messi.jpg` | Lionel Messi | 216,906 | [File:Lionel Messi in 2018.jpg](https://commons.wikimedia.org/wiki/File:Lionel_Messi_in_2018.jpg) | Кирилл Венедиктов ([soccer.ru](https://www.soccer.ru/galery/1055457/photo/733494)) | [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0) |

## Attribution

All three files are CC-BY / CC-BY-SA and **require attribution on reuse**. The rows above
carry the author, the source link and the licence; keep them with the images if you copy
them out of this repository.

## Detector check

Each file was chosen to be a **single-subject portrait with a large primary face**, and
verified by running the real stage 1 detector over it:

```
$ python -m facechain.face samples/<file>

elon-musk.jpg:     faces=1  conf=0.852  bbox=[282, 233, 478, 622]
lionel-messi.jpg:  faces=1  conf=0.923  bbox=[103,  78, 181, 265]
sundar-pichai.jpg: faces=1  conf=0.838  bbox=[258, 320, 456, 556]
```

`faces=1` is the point. A group photo makes the pipeline encode whichever face happens to
be largest, and a small crop of the wrong person is a silently wrong demo rather than a
loud failure. Candidates with more than a couple of faces, or a primary bounding box
narrower than roughly 150 px, were rejected during selection.

Face geometry is necessary but not sufficient: a two-person "X meets Y" photo on Commons
can be cropped to **Y**, and it still detects one large, confident face. The Pichai sample
was briefly wrong for exactly that reason. Every file here has now been eyeballed, not
just measured.

Wikimedia Commons is one of the search providers in stage 2. That is not a shortcut — the
input file is never passed to the search layer, only its 128-d embedding is, and any
Commons result still has to be downloaded, re-detected, re-encoded and clear the cosine
threshold like every other candidate.

## Usage

```bash
python run_pipeline.py --image samples/elon-musk.jpg --hint "Elon Musk"
```
