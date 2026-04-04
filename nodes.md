
items (folder, feed, etc., just lots of xml entries) -> 

- REGEX REPLACE and FILTER
- fuzzy match (edit distance)
- intersection
- set minus (all things in A and not B)
- apply keyword filtering (exclude and include?)
  - local llm that i give it a prompt and it filters
     - give it full text
  - embedding similarity to X
      - gwern sorting similarity thingy
  - summarize
- attach a xml keyword to the item
   - extract youtube transcript
   - can be based on existing
- modify an existing keyword
- split by keyword emits lots of lists
- size filtering of list
- join each list recieved together
- join until size over k and then emit
- sort by keyword (given comparison)
- add extra feed based on field
    - 
- dedup
- output to feed (which can then be input)
- process
   - takes an item and adds additional data based on contents of the item
       - 

- local llm modify headline less clickbait

- input -> all things i have liked (feed?)
   - extract who liked them, add that as entry to each of them
   - input ^ and output ppl
   - join
   - dedup
   - input ^ output feeds for each like made by those ppl  -> liked posts shared with ppl with shared likes
- process:
    - full the whole thing local
- send email each day or telegram zulip etc.
- watches
   - feeds that ignore all sources and 


liked posts shared with ppl with shared likes
  - dup counts 


discovery?
- what would not do discovery?
    - likes by ppl i agree with
        - add that score, along with engagement
- retweets do a lot of it
- get for you feed of x person
- get likes of x person
- judgers different than producers
- things i like, see who retweets them or likes or is in their feeds too
- local llm that i give it a prompt and it filters