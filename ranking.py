# few ways of interacting:
# - gather some posts (or enter keywords), each weighted (including negative), they define a score based on items that are most similar to them in embedding space
# - we could filter or rank based on that score
# - we can also cluster the feed based on embeddings - could be nice to see a umap and click?
#     - is a score, where
# 1. Take firehose
# 2. Do pac-umap
# 3. We look at it and select items that represent a group
# 4. That gives us score which is distance from that group
#   (also tells us what keywords are most similar to that group)
# Tool to pick what order we feed content in
#     if i show u a post with word vector d, next post should not be close to d
# Score that is interaction relative to baseline
#    but how do we deal with new stuff not populating metrics for a time?
#       one option is to estimate curve over time? (if day old, double views etc.)
#       simpler option is just to not show it for 1-2 days (user can pick delay, even just a few hours is probably good)
#
# Two steps: ranking and filtering


# data and code from https://github.com/stanfordnlp/GloVe
def load_word_vecs():
    with open(args.vocab_file, 'r') as f:
        words = [x.rstrip().split(' ')[0] for x in f.readlines()]
    with open(args.vectors_file, 'r') as f:
        vectors = {}
        for line in f:
            vals = line.rstrip().split(' ')
            vectors[vals[0]] = [float(x) for x in vals[1:]]

    vocab_size = len(words)
    vocab = {w: idx for idx, w in enumerate(words)}
    ivocab = {idx: w for idx, w in enumerate(words)}

    vector_dim = len(vectors[ivocab[0]])
    W = np.zeros((vocab_size, vector_dim))
    for word, v in vectors.items():
        if word == '<unk>':
            continue
        W[vocab[word], :] = v

    # normalize each word vector to unit variance
    W_norm = np.zeros(W.shape)
    d = (np.sum(W ** 2, 1) ** (0.5))
    W_norm = (W.T / d).T
    return (W_norm, vocab, ivocab)


