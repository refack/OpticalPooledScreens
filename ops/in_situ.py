import numpy as np
import pandas as pd
from ops.constants import *
import ops.utils

IMAGING_ORDER = 'GTAC'


def extract_base_intensity(maxed, peaks, cells, threshold_std):

    # only analyze reads inside cells 
    read_mask = (peaks > threshold_std) & (cells > 0)
    values = maxed[:, :, read_mask].transpose([2, 0, 1])
    labels = cells[read_mask]
    positions = np.array(np.where(read_mask)).T

    return values, labels, positions


def format_bases(values, labels, positions, cycles, bases):    
    index = (CYCLE, cycles), (CHANNEL, bases)
    try:
        df = ops.utils.ndarray_to_dataframe(values, index)
    except ValueError:
        print('failed to reshape peaks to sequencing bases, writing empty table')
        return pd.DataFrame()

    df_positions = pd.DataFrame(positions, columns=[POSITION_I, POSITION_J])
    df = (df.stack([CYCLE, CHANNEL])
       .reset_index()
       .rename(columns={0: INTENSITY, 'level_0': READ})
       .join(pd.Series(labels, name=CELL), on=READ)
       .join(df_positions, on=READ)
       .sort_values([CELL, READ, CYCLE])
       )

    return df


def do_median_call(df_bases, cycles=12, channels=4):
  X = dataframe_to_values(df_bases, channels=channels)
  Y, W = transform_medians(X.reshape(-1, channels))

  df_reads = call_barcodes(df_bases, Y, cycles=cycles, channels=channels)
  return df_reads.drop([CHANNEL, INTENSITY], axis=1)


def clean_up_bases(df_bases):
    """Sort. Pre-processing for `dataframe_to_values`.
    """
    return df_bases.sort_values([WELL, TILE, CELL, READ, CYCLE, CHANNEL])


def call_cells(df_reads):
    """Determine count of top barcodes 
    """
    cols = [WELL, TILE, CELL]
    s = (df_reads
       .drop_duplicates([WELL, TILE, READ])
       .groupby(cols)[BARCODE]
       .value_counts()
       .rename('count')
       .sort_values(ascending=False)
       .reset_index()
       .groupby(cols)
        )

    return (df_reads
      .join(s.nth(0)[BARCODE].rename(BARCODE_0),                 on=cols)
      .join(s.nth(0)['count'].rename(BARCODE_COUNT_0).fillna(0), on=cols)
      .join(s.nth(1)[BARCODE].rename(BARCODE_1),                 on=cols)
      .join(s.nth(1)['count'].rename(BARCODE_COUNT_1).fillna(0), on=cols)
      .join(s['count'].sum() .rename(BARCODE_COUNT),             on=cols)
      .drop_duplicates(cols)
      .drop([BARCODE], axis=1) # drop the read barcode
    )


def dataframe_to_values(df, value='intensity', channels=4):
    """Dataframe must be sorted on [cycles, channels]. 
    Returns N x cycles x channels.
    """
    cycles = df['cycle'].value_counts()
    assert len(set(cycles)) == 1
    n_cycles = len(cycles)
    x = np.array(df[value]).reshape(-1, n_cycles, channels)
    return x


def transform_medians(X):
    """For each dimension, find points where that dimension is max. Use median of those points to define new axes. 
    Describe with linear transformation W so that W * X = Y.
    """

    def get_medians(X):
        arr = []
        for i in range(X.shape[1]):
            arr += [np.median(X[X.argmax(axis=1) == i], axis=0)]
        M = np.array(arr)
        return M

    M = get_medians(X).T
    M = M / M.sum(axis=0)
    W = np.linalg.inv(M)
    Y = W.dot(X.T).T.astype(int)
    return Y, W


def call_barcodes(df_bases, Y, cycles=12, channels=4):
    bases = sorted(IMAGING_ORDER[:channels])
    df_reads = df_bases.drop_duplicates([WELL, TILE, READ]).copy()
    df_reads[CYCLES_IN_SITU] = call_bases_fast(Y.reshape(-1, cycles, channels), bases)
    Q = quality(Y.reshape(-1, cycles, channels))
    # needed for performance later
    for i in range(len(Q[0])):
        df_reads['Q_%d' % i] = Q[:,i]
 
    # cycles converted straight to barcodes
    df_reads = df_reads.rename(columns={CYCLES_IN_SITU: BARCODE})
    df_reads['Q_min'] = df_reads.filter(regex='Q_\d+').min(axis=1)
    return df_reads


def call_bases_fast(values, bases):
    """4-color: bases='ACGT'
    """
    assert values.ndim == 3
    assert values.shape[2] == len(bases)
    calls = values.argmax(axis=2)
    calls = np.array(list(bases))[calls]
    return [''.join(x) for x in calls]


def quality(X):
    X = np.abs(np.sort(X, axis=-1).astype(float))
    Q = 1 - np.log(2 + X[..., -2]) / np.log(2 + X[..., -1])
    Q = (Q * 2).clip(0, 1)
    return Q


def reads_to_fastq(df, microscope='MN', dataset='DS', flowcell='FC'):

    wrap = lambda x: '{' + x + '}'
    join_fields = lambda xs: ':'.join(map(wrap, xs))

    a = '@{m}:{d}:{f}'.format(m=microscope, d=dataset, f=flowcell)
    b = join_fields([WELL, CELL, 'well_tile', READ, POSITION_I, POSITION_J])
    c = '\n{b}\n+\n{{phred}}'.format(b=wrap(BARCODE))
    fmt = a + b + c 
    
    well_tiles = sorted(set(df[WELL] + '_' + df[TILE]))
    fields = [WELL, TILE, CELL, READ, POSITION_I, POSITION_J, BARCODE]
    
    Q = df.filter(like='Q_').values
    
    reads = []
    for i, row in enumerate(df[fields].values):
        d = dict(zip(fields, row))
        d['phred'] = ''.join(phred(q) for q in Q[i])
        d['well_tile'] = well_tiles.index(d[WELL] + '_' + d[TILE])
        reads.append(fmt.format(**d))
    
    return reads
    

def dataframe_to_fastq(df, file, dataset):
    s = '\n'.join(reads_to_fastq(df, dataset))
    with open(file, 'w') as fh:
        fh.write(s)
        fh.write('\n')


def phred(q):
    """Convert 0...1 to 0...30
    No ":".
    No "@".
    No "+".
    """
    n = int(q * 30 + 33)
    if n == 43:
        n += 1
    if n == 58:
        n += 1
    return chr(n)


def add_clusters(df_cells, neighbor_dist=50):
    """Assigns -1 to clusters with only one cell.
    """
    from scipy.spatial.kdtree import KDTree
    import networkx as nx

    x = df_cells[GLOBAL_X] + df_cells[POSITION_J]
    y = df_cells[GLOBAL_Y] + df_cells[POSITION_I]
    barcodes = df_cells[BARCODE_0]
    barcodes = np.array(barcodes)

    kdt = KDTree(np.array([x, y]).T)
    num_cells = len(df_cells)
    print('searching for clusters among %d cells' % num_cells)
    pairs = kdt.query_pairs(neighbor_dist)
    pairs = np.array(list(pairs))

    x = barcodes[pairs]
    y = x[:, 0] == x[:, 1]

    G = nx.Graph()
    G.add_edges_from(pairs[y])

    clusters = list(nx.connected_components(G))

    cluster_index = np.zeros(num_cells, dtype=int) - 1
    for i, c in enumerate(clusters):
        cluster_index[list(c)] = i

    df_cells[CLUSTER] = cluster_index
    return df_cells
