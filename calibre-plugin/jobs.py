#!/usr/bin/env python
# vim:fileencoding=UTF-8:ts=4:sw=4:sta:et:sts=4:ai
from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

__license__   = 'GPL v3'
__copyright__ = '2015, Jim Miller, 2011, Grant Drake <grant.drake@gmail.com>'
__docformat__ = 'restructuredtext en'

import logging
logger = logging.getLogger(__name__)

import time, traceback
from StringIO import StringIO
    
from calibre.utils.ipc.server import Server
from calibre.utils.ipc.job import ParallelJob
from calibre.constants import numeric_version as calibre_version

# ------------------------------------------------------------------------------
#
#              Functions to perform downloads using worker jobs
#
# ------------------------------------------------------------------------------

def do_download_worker(book_list, options,
                       cpus, notification=lambda x,y:x):
    '''
    Master job, to launch child jobs to extract ISBN for a set of books
    This is run as a worker job in the background to keep the UI more
    responsive and get around the memory leak issues as it will launch
    a child job for each book as a worker process
    '''
    server = Server(pool_size=cpus)

    logger.info(options['version'])
    total = 0
    alreadybad = []
    # Queue all the jobs
    logger.info("Adding jobs for URLs:")
    for book in book_list:
        logger.info("%s"%book['url'])
        if book['good']:
            total += 1
            args = ['calibre_plugins.fanficfare_plugin.jobs',
                    'do_download_for_worker',
                    (book,options)]
            job = ParallelJob('arbitrary_n',
                              "url:(%s) id:(%s)"%(book['url'],book['calibre_id']),
                              done=None,
                              args=args)
            job._book = book
            server.add_job(job)
        else:
            # was already bad before the subprocess ever started.
            alreadybad.append(book)
    
    # This server is an arbitrary_n job, so there is a notifier available.
    # Set the % complete to a small number to avoid the 'unavailable' indicator
    notification(0.01, _('Downloading FanFiction Stories'))

    # dequeue the job results as they arrive, saving the results
    count = 0
    while True:
        job = server.changed_jobs_queue.get()
        # A job can 'change' when it is not finished, for example if it
        # produces a notification. Ignore these.
        job.update()
        if not job.is_finished:
            continue
        # A job really finished. Get the information.
        book_list.remove(job._book)
        book_list.append(job.result)
        book_id = job._book['calibre_id']
        count = count + 1
        notification(float(count)/total, '%d of %d stories finished downloading'%(count,total))
        # Add this job's output to the current log
        logger.info('Logfile for book ID %s (%s)'%(book_id, job._book['title']))
        logger.info(job.details)

        if count >= total:
            logger.info("\n"+_("Successful:")+"\n%s\n"%("\n".join([book['url'] for book in 
                                                      filter(lambda x: x['good'], book_list) ] ) ) )
            logger.info("\n"+_("Unsuccessful:")+"\n%s\n"%("\n".join([book['url'] for book in 
                                                        filter(lambda x: not x['good'], book_list) ] ) ) )
            break

    server.close()
    
    # return the book list as the job result
    return book_list

def do_download_for_worker(book,options,notification=lambda x,y:x):
    '''
    Child job, to download story when run as a worker job
    '''

    from calibre_plugins.fanficfare_plugin import FanFicFareBase
    fffbase = FanFicFareBase(options['plugin_path'])
    with fffbase:
        
        from calibre_plugins.fanficfare_plugin.dialogs import (NotGoingToDownload,
                OVERWRITE, OVERWRITEALWAYS, UPDATE, UPDATEALWAYS, ADDNEW, SKIP, CALIBREONLY, CALIBREONLYSAVECOL)
        from calibre_plugins.fanficfare_plugin.fanficfare import adapters, writers, exceptions
        from calibre_plugins.fanficfare_plugin.fanficfare.epubutils import get_update_data
        
        from calibre_plugins.fanficfare_plugin.fff_util import (get_fff_adapter, get_fff_config)
        
        try:
            book['comment'] = _('Download started...')
            
            configuration = get_fff_config(book['url'],
                                            options['fileform'],
                                            options['personal.ini'])

            if configuration.getConfig('use_ssl_unverified_context'):
                ## monkey patch to avoid SSL bug.  dupliated from
                ## fff_plugin.py because bg jobs run in own process
                ## space.
                import ssl
                if hasattr(ssl, '_create_unverified_context'):
                    ssl._create_default_https_context = ssl._create_unverified_context
    
            if not options['updateepubcover'] and 'epub_for_update' in book and options['collision'] in (UPDATE, UPDATEALWAYS):
                configuration.set("overrides","never_make_cover","true")
    
            # images only for epub, html, even if the user mistakenly
            # turned it on else where.
            if options['fileform'] not in ("epub","html"):
                configuration.set("overrides","include_images","false")
            
            adapter = adapters.getAdapter(configuration,book['url'])
            adapter.is_adult = book['is_adult'] 
            adapter.username = book['username'] 
            adapter.password = book['password']
            adapter.setChaptersRange(book['begin'],book['end'])
            
            adapter.load_cookiejar(options['cookiejarfile'])
            logger.debug("cookiejar:%s"%adapter.cookiejar)
            adapter.set_pagecache(options['pagecache'])
            
            story = adapter.getStoryMetadataOnly()
            if 'calibre_series' in book:
                adapter.setSeries(book['calibre_series'][0],book['calibre_series'][1])
                
            # set PI version instead of default.
            if 'version' in options:
                story.setMetadata('version',options['version'])
                
            writer = writers.getWriter(options['fileform'],configuration,adapter)
    
            outfile = book['outfile']
    
            ## No need to download at all.  Shouldn't ever get down here.
            if options['collision'] in (CALIBREONLY, CALIBREONLYSAVECOL):
                logger.info("Skipping CALIBREONLY 'update' down inside worker--this shouldn't be happening...")
                book['comment'] = 'Metadata collected.'
                book['all_metadata'] = story.getAllMetadata(removeallentities=True)
                book['savemetacol'] = story.dump_html_metadata()
                
            ## checks were done earlier, it's new or not dup or newer--just write it.
            elif options['collision'] in (ADDNEW, SKIP, OVERWRITE, OVERWRITEALWAYS) or \
                    ('epub_for_update' not in book and options['collision'] in (UPDATE, UPDATEALWAYS)):
    
                # preserve logfile even on overwrite.
                if 'epub_for_update' in book:
                    
                    adapter.logfile = get_update_data(book['epub_for_update'])[6]
                    # change the existing entries id to notid so
                    # write_epub writes a whole new set to indicate overwrite.
                    if adapter.logfile:
                        adapter.logfile = adapter.logfile.replace("span id","span notid")
                
                logger.info("write to %s"%outfile)
                inject_cal_cols(book,story,configuration)
                writer.writeStory(outfilename=outfile, forceOverwrite=True)
                book['comment'] = 'Download %s completed, %s chapters.'%(options['fileform'],story.getMetadata("numChapters"))
                book['all_metadata'] = story.getAllMetadata(removeallentities=True)
                book['savemetacol'] = story.dump_html_metadata()
                
            ## checks were done earlier, just update it.
            elif 'epub_for_update' in book and options['collision'] in (UPDATE, UPDATEALWAYS):
    
                # update now handled by pre-populating the old images and
                # chapters in the adapter rather than merging epubs.
                urlchaptercount = int(story.getMetadata('numChapters').replace(',',''))
                (url,
                 chaptercount,
                 adapter.oldchapters,
                 adapter.oldimgs,
                 adapter.oldcover,
                 adapter.calibrebookmark,
                 adapter.logfile) = get_update_data(book['epub_for_update'])[0:7]
    
                # dup handling from fff_plugin needed for anthology updates.
                if options['collision'] == UPDATE:
                    if chaptercount == urlchaptercount:
                        book['comment']=_("Already contains %d chapters.  Reuse as is.")%chaptercount
                        book['all_metadata'] = story.getAllMetadata(removeallentities=True)
                        book['savemetacol'] = story.dump_html_metadata()
                        book['outfile'] = book['epub_for_update'] # for anthology merge ops.
                        return book
    
                # dup handling from fff_plugin needed for anthology updates.
                if chaptercount > urlchaptercount:
                    raise NotGoingToDownload(_("Existing epub contains %d chapters, web site only has %d. Use Overwrite to force update.") % (chaptercount,urlchaptercount),'dialog_error.png')
    
                if not (options['collision'] == UPDATEALWAYS and chaptercount == urlchaptercount) \
                        and adapter.getConfig("do_update_hook"):
                    chaptercount = adapter.hookForUpdates(chaptercount)
    
                logger.info("Do update - epub(%d) vs url(%d)" % (chaptercount, urlchaptercount))
                logger.info("write to %s"%outfile)
    
                inject_cal_cols(book,story,configuration)
                writer.writeStory(outfilename=outfile, forceOverwrite=True)
                
                book['comment'] = _('Update %s completed, added %s chapters for %s total.')%\
                    (options['fileform'],(urlchaptercount-chaptercount),urlchaptercount)
                book['all_metadata'] = story.getAllMetadata(removeallentities=True)
                book['savemetacol'] = story.dump_html_metadata()
            
            if options['smarten_punctuation'] and options['fileform'] == "epub" \
                    and calibre_version >= (0, 9, 39):
                # for smarten punc
                from calibre.ebooks.oeb.polish.main import polish, ALL_OPTS
                from calibre.utils.logging import Log
                from collections import namedtuple

                # do smarten_punctuation from calibre's polish feature
                data = {'smarten_punctuation':True}
                opts = ALL_OPTS.copy()
                opts.update(data)
                O = namedtuple('Options', ' '.join(ALL_OPTS.iterkeys()))
                opts = O(**opts)
                
                log = Log(level=Log.DEBUG)
                # report = []
                polish({outfile:outfile}, opts, log, logger.info) # report.append
            
        except NotGoingToDownload as d:
            book['good']=False
            book['comment']=unicode(d)
            book['icon'] = d.icon
    
        except Exception as e:
            book['good']=False
            book['comment']=unicode(e)
            book['icon']='dialog_error.png'
            book['status'] = 'Error'
            logger.info("Exception: %s:%s"%(book,unicode(e)))
            traceback.print_exc()
            
        #time.sleep(10)
    return book

## calibre's columns for an existing book are pased in and injected
## into the story's metadata.  For convenience, we also add labels and
## valid_entries for them in a special [injected] section that has
## even less precedence than [defaults]
def inject_cal_cols(book,story,configuration):
    configuration.remove_section('injected')
    if 'calibre_columns' in book:
        injectini = ['[injected]']
        extra_valid = []
        for k, v in book['calibre_columns'].iteritems():
            story.setMetadata(k,v['val'])
            injectini.append('%s_label:%s'%(k,v['label']))
            extra_valid.append(k)
        if extra_valid: # if empty, there's nothing to add.
            injectini.append("add_to_extra_valid_entries:,"+','.join(extra_valid))
            configuration.readfp(StringIO('\n'.join(injectini)))
            #print("added:\n%s\n"%('\n'.join(injectini)))
    
