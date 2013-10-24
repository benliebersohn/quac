# Copyright (c) 2012-2013 Los Alamos National Security, LLC, and others.
# -*- coding: utf-8 -*-

'''For each input n-gram occurring more than min_occur number of times,
   compute a time series of occurrences per day.

   Each output item's key is the n-gram, and the value is a dictionary with
   the following structure::

     { 'ngram':      <the ngram as a unicode object>,
       'total':      <total number of occurrences as an integer>,
       'series':     <Date_Vector containing the time series> }

   The n-gram is redundantly encoded (as the key and as part of the value).
   This is so that values can be independently used without retaining the
   corresponding key.

   Dates in this pipeline are very weird -- they are the "proleptic Gregorian
   ordinal" of the date (http://docs.python.org/2/library/datetime.html)
   represented as an integer. That is, the 1-based day number starting at
   January 1 in the year 1. The ordinal of 2012-10-21 is 734,797. This is for
   performance reasons (parsing a date is about 200 times slower than parsing
   an int, and we do it once per input line).'''


import collections
import datetime
import glob
import gzip
import itertools
import operator
import sys
import urllib

import numpy as np

from . import base
import math_
import ssheet
import tok.unicode_props
import tsv_glue
import tweet
import u
import wikimedia


class Build_Job(base.TSV_Internal_Job, base.KV_Pickle_Seq_Output_Job):

   def reduce(self, ngram, datecounts):
      cts = collections.Counter()
      first_day = float('+inf')
      last_day = float('-inf')
      for (date, count) in datecounts:
         date = int(date)
         count = int(count)
         first_day = min(first_day, date)
         last_day = max(last_day, date)
         cts[date] += count
      total = sum(cts.itervalues())
      if (total >= self.params['min_occur']):
         assert (first_day <= last_day)
         # use float32 for space efficiency at the expense of precision
         first_dt = datetime.date.fromordinal(first_day)
         last_dt = datetime.date.fromordinal(last_day)
         ct_series = math_.Date_Vector.zeros(first_dt, last_dt,
                                             dtype=np.float32)
         for (date, ct) in cts.iteritems():
            ct_series[date - first_day] = ct
         yield (ngram, { 'ngram': ngram,
                         'total': total,
                         'series': ct_series })


class Correlate_Job(base.KV_Pickle_Seq_Input_Job, base.TSV_Output_Job):

   def map_init(self):
      self.totals = u.pickle_load(self.params['total_file'])
      # Compute masks (find days with insufficient data)
      for (proj, pdata) in self.totals.iteritems():
         if (proj != 't@'):
            # Wikipedia - no masking needed
            pdata['mask'] = None
         else:
            # Twitter - data have holes, so compute masks
            mask = [tweet.is_enough(pdata['series'].date(i),
                                    pdata['series'][i],
                                    sample_rate=self.params['tw_sample_rate'])
                    for i in xrange(len(pdata['series']))]
            mask = np.array(mask, dtype=bool)
            if (mask.sum() < 0.5 * len(mask)):
               u.abort('too many low-data days (%d of %d); check sample rate?'
                       % (mask.sum(), len(mask)))
            pdata['mask'] = math_.Date_Vector(pdata['series'].first_day, mask)
      # Read target time series
      self.targets = list()
      short_names = u.without_common_prefix(self.params['input_sss'])
      for (sn, ln) in zip(short_names, self.params['input_sss']):
         e = ssheet.Excel(file_=ln)
         for (name, (series, mask)) in e.data.iteritems():
            name = '%s:%s' % (urllib.quote_plus(u.without_ext(sn, '.xls')),
                              urllib.quote_plus(name))
            self.targets.append({ 'name':   name,
                                  'series': series,
                                  'mask':   mask })

   def map(self, kv):
      (_, ngram) = kv
      for t in self.targets:
         # Extend the ngram series to match the target, to make sure that
         # leading and trailing zeroes are not lost.
         ng_vec = ngram['series'].grow_to(t['series'])
         proj = ngram['ngram'].split(' ')[0]
         ng_vec = ng_vec.normalize(self.totals[proj]['series'], parts_per=1e6)
         peak = ng_vec.max()
         trough = ng_vec.min()
         # Ignore series with a peak that is too low.
         if (peak < self.params['min_ppm']):
            continue
         # Compute correlation.
         assert (t['series'].bounds_eq(t['mask']))
         r = math_.pearson(ng_vec, t['series'],
                           self.totals[proj]['mask'], t['mask'])
         if (abs(r) >= self.params['min_similarity']):
            yield (t['name'], (ngram['ngram'], r, peak, trough))

   def reduce_open_output(self):
      # output is opened in reduce()
      pass

   def reduce(self, target_series_name, matches):
      # Pretty much the only "real" work here is sorting matches.
      def abs1(x):
         return abs(x[1])  # element 1 is the correlation
      # Output here is kind of awkward. We want one TSV output file per XLS
      # input file, which corresponds to one key, so we re-open the output
      # stream each time reduce() is called. We also have to no-op
      # reduce_open_output() above, which seems strange to me.
      self.outfp = tsv_glue.Writer('%s/%s.tsv' % (self.outdir,
                                                  target_series_name),
                                   clobber=True, buffering=base.OUTPUT_BUFSIZE)
      for m in sorted(matches, key=abs1, reverse=True):
         yield m


class Tweet_Job(base.TSV_Input_Job, Build_Job):

   def __init__(self, params):
      base.Job.__init__(self, params)
      self.tzer = tok.unicode_props.UP_Tiny(self.params['n'])

   def map(self, fields):
      # WARNING: make sure field indices match any file format changes
      #
      # Note: While we have an iso8601_parse() method in time_, it is kind of
      # slow (~60 μs per parse on my box) and contains a number of
      # contingencies we don't need here. In past experience, parsing dates
      # once per input line consumes quite a lot of the total time, so we want
      # to be expedient here (this approach takes ~9.5 µs). Another option
      # would be to reprocess the Twitter data to include proleptic Gregorian
      # ordinals directly (parsing ints takes only 250 ns).
      date = str(datetime.datetime.strptime(fields[1][:10],  # date only
                                            '%Y-%m-%d').toordinal())
      for token in self.tzer.tokenize(fields[2]):  # tweet text
         yield (('t@ ' + token).encode('utf8'), (date, '1'))


class Wikimedia_Job(Build_Job):

   # A note on decoding article names in URLs: We don't do it because (a) it
   # saves significant time and (b) there are apparently non-UTF-8 encodings
   # in use. I believe the URL encoding is selected by the browser.
   #
   # An artifact of (b) is that article counts can be split. For example, the
   # Russian article Люди_Икс (i.e., the X-Men comic series) can be accessed
   # at both of the following URLs:
   #
   #   (UTF-8)        http://ru.wikipedia.org/wiki/%D0%9B%D1%8E%D0%B4%D0%B8_%D0%98%D0%BA%D1%81
   #   (Windows-1251) http://ru.wikipedia.org/wiki/%CB%FE%E4%E8_%C8%EA%F1
   #
   # Other encodings (e.g., ISO 8859-5: %BB%EE%D4%D8_%B8%DA%E1 and KOI8-R,
   # %EC%C0%C4%C9_%E9%CB%D3) do not work. Figuring out this mess is something
   # I'm not very interested in.
   #
   # We do, however, normalize spaces into underscores. I believe this may be
   # incomplete (see issue #77).

   def map(self, dirname):
      '''This mapper is a bit odd. Rather than actual content, it accepts
         simply a directory name, then opens and emits the content of each
         gzipped file in that directory. While this limits parallelism, it
         reduces the number of dependencies to manage in the job (there are
         currently about 50,000) pageview files in the dataset, which we
         multiply by the number of reducers (50k * 256 = 12.8 million).

         We do this for performance reasons. It saves several I/O steps to
         embed the reading in the mapper rather than having a very simple
         mapper. This is likely to break Hadoop Streaming compatibility, but
         since it's so simple, it's easy to re-do if we go direction.'''
      for filename in glob.glob(dirname + '/pagecounts-*.gz'):
         date = str(wikimedia.timestamp_parse(filename).toordinal())
         for line in gzip.open(filename, 'rb'):
            fields = line.split(' ')
            project = fields[0]
            article = fields[1].replace('%20', '_')  # see issue #77
            count = fields[2]
            yield (project + ' ' + article, (date, count))
