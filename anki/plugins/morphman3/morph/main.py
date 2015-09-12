import time

from anki.utils import splitFields, joinFields, stripHTML, intTime, fieldChecksum
from morphemes import MorphDb, AnkiDeck, getMorphemes
import stats
from util import printf, mw, memoize, cfg, cfg1, partial, errorMsg, infoMsg
import util

@memoize
def getFieldIndex( fieldName, mid ):
    m = mw.col.models.get( mid )
    d = dict( ( f['name'], f['ord'] ) for f in m['flds'] )
    try: return d[ fieldName ]
    except KeyError: return None

def getMecabField( fname, flds, mid ):
    idx = getFieldIndex( fname, mid )
    return stripHTML( splitFields( flds )[ idx ] )

@memoize
def getSortFieldIndex( mid ):
    return mw.col.models.get( mid )[ 'sortf' ]

def setField( mid, fs, k, v ): # nop if field DNE
    idx = getFieldIndex( k, mid )
    if idx: fs[ idx ] = v

def mkAllDb( allDb=None ):
    t_0, db, TAG = time.time(), mw.col.db, mw.col.tags
    N_notes = db.scalar( 'select count() from notes' )
    mw.progress.start( label='Prep work for all.db creation', max=N_notes, immediate=True )

    if not allDb: allDb = MorphDb()
    fidDb   = allDb.fidDb()
    locDb   = allDb.locDb( recalc=False )   # fidDb() already forces locDb recalc

    mw.progress.update( label='Generating all.db data' )
    for i,( nid, mid, flds, guid, tags ) in enumerate( db.execute( 'select id, mid, flds, guid, tags from notes' ) ):
        if i % 500 == 0:    mw.progress.update( value=i )
        C = partial( cfg, mid, None )
        if isAnalysisEnabled(C): continue
        
        mats = [ ( 0.5 if ivl == 0 and ctype == 1 else ivl ) for ivl, ctype in db.execute( 'select ivl, type from cards where nid = :nid', nid=nid ) ]
        if C('ignore maturity'):
            mats = [ 0 for mat in mats ]
        ts, alreadyKnownTag = TAG.split( tags ), C('tag_alreadyKnown')
        if alreadyKnownTag in ts:
            mats += [ C('threshold_mature')+1 ]

        for fieldName in C('morph_fields'):
            try: # if doesn't have field, continue
                #fieldValue = normalizeFieldValue( getField( fieldName, flds, mid ) )
                fieldValue = getMecabField( fieldName, flds, mid )
            except KeyError: continue
            except TypeError:
                mname = mw.col.models.get( mid )[ 'name' ]
                errorMsg( u'Failed to get field "{field}" from a note of model "{model}". Please fix your config.py file to match your collection appropriately and ignore the following error.'.format( model=mname, field=fieldName ) )
                raise

            loc = fidDb.get( ( nid, guid, fieldName ), None )
            if not loc:
                loc = AnkiDeck( nid, fieldName, fieldValue, guid, mats )
                ms = getMorphemes( fieldValue )
                if ms: #TODO: this needed? should we change below too then?
                    #printf( '    .loc for %d[%s]' % ( nid, fieldName ) )
                    locDb[ loc ] = ms
            else:
                # mats changed -> new loc (new mats), move morphs
                if loc.fieldValue == fieldValue and loc.maturities != mats:
                    printf( '    .mats for %d[%s]' % ( nid, fieldName ) )
                    newLoc = AnkiDeck( nid, fieldName, fieldValue, guid, mats )
                    locDb[ newLoc ] = locDb.pop( loc )
                # field changed -> new loc, new morphs
                elif loc.fieldValue != fieldValue:
                    printf( '    .morphs for %d[%s]' % ( nid, fieldName ) )
                    newLoc = AnkiDeck( nid, fieldName, fieldValue, guid, mats )
                    ms = getMorphemes( fieldValue )
                    locDb.pop( loc )
                    locDb[ newLoc ] = ms
    printf( 'Processed all %d notes in %f sec' % ( N_notes, time.time() - t_0 ) )
    mw.progress.update( value=i, label='Creating all.db object' )
    allDb.clear()
    allDb.addFromLocDb( locDb )
    if cfg1('saveDbs'):
        mw.progress.update( value=i, label='Saving all.db to disk' )
        allDb.save( cfg1('path_all') )
        printf( 'Processed all %d notes + saved all.db in %f sec' % ( N_notes, time.time() - t_0 ) )
    mw.progress.finish()
    return allDb

def isAnalysisEnabled(noteConfig):
	return ( not noteConfig('enabled') and not noteConfig('analyze'))

def filterDbByMat( db, mat ):
    '''Assumes safe to use cached locDb'''
    newDb = MorphDb()
    for loc, ms in db.locDb( recalc=False ).iteritems():
        if loc.maturity > mat:
            newDb.addMsL( ms, loc )
    return newDb

def updateNotes( allDb ):
    t_0, now, db, TAG   = time.time(), intTime(), mw.col.db, mw.col.tags
    ds, nid2mmi         = [], {}
    N_notes             = db.scalar( 'select count() from notes' )
    mw.progress.start( label='Updating data', max=N_notes, immediate=True )
    fidDb   = allDb.fidDb()
    locDb   = allDb.locDb( recalc=False ) # fidDb() already forces locDb recalc

    # handle secondary databases
    mw.progress.update( label='Creating seen/known/mature from all.db' )
    seenDb      = filterDbByMat( allDb, cfg1('threshold_seen') )
    knownDb     = filterDbByMat( allDb, cfg1('threshold_known') )
    matureDb    = filterDbByMat( allDb, cfg1('threshold_mature') )
    mw.progress.update( label='Loading priority.db' )
    priorityDb  = MorphDb( cfg1('path_priority'), ignoreErrors=True ).db

    if cfg1('saveDbs'):
        mw.progress.update( label='Saving seen/known/mature dbs' )
        seenDb.save( cfg1('path_seen') )
        knownDb.save( cfg1('path_known') )
        matureDb.save( cfg1('path_mature') )

    mw.progress.update( label='Calculating frequency information' )
    pops = [ len( locs ) for locs in allDb.db.values() ]
    pops = [ n for n in pops if n > 1 ]

    mw.progress.update( label='Updating notes' )
    for i,( nid, mid, flds, guid, tags ) in enumerate( db.execute( 'select id, mid, flds, guid, tags from notes' ) ):
        if i % 500 == 0:    mw.progress.update( value=i )
        C = partial( cfg, mid, None )
        if not C('enabled'): continue
        # Get all morphemes for note
        ms = set()
        for fieldName in C('morph_fields'):
            try:
                loc = fidDb[ ( nid, guid, fieldName ) ]
                ms.update( locDb[ loc ] )
            except KeyError: continue
        ms = [ m for m in ms if m.pos not in C('morph_blacklist') ]

        # Determine un-seen/known/mature and i+N
        unseens, unknowns, unmatures, newKnowns = set(), set(), set(), set()
        for m in ms:
            if m not in seenDb.db:      unseens.add( m )
            if m not in knownDb.db:     unknowns.add( m )
            if m not in matureDb.db:    unmatures.add( m )
            if m not in matureDb.db and m in knownDb.db:
                newKnowns.add( m )

        # Determine MMI - Morph Man Index
        N, N_s, N_k, N_m = len( ms ), len( unseens ), len( unknowns ), len( unmatures )

        # Bail early for lite update
        if N_k > 2 and C('only update k+2 and below'): continue

            # average frequency of unknowns (ie. how common the word is within your collection)
        F_k = 0
        for focusMorph in unknowns: # focusMorph used outside loop
            F_k += len( allDb.db[ focusMorph ] )
        F_k_avg = F_k / N_k if N_k > 0 else F_k
        usefulness = F_k_avg

            # add bonus for morphs in priority.db
        isPriority = False
        for focusMorph in unknowns:
            if focusMorph in priorityDb:
                isPriority = True
                usefulness += C('priority.db weight')

            # add bonus for studying recent learned knowns (reinforce)
        for m in newKnowns:
            locs = allDb.db[ m ]
            if locs:
                ivl = min( 1, max( loc.maturity for loc in locs ) )
                usefulness += C('reinforce new vocab weight') / ivl #TODO: maybe average this so it doesnt favor long sentences

        if any( m.pos == u'動詞' for m in unknowns ): #FIXME: this isn't working???
            usefulness += C('verb bonus')

        usefulness = 999 - min( 999, usefulness )

            # difference from optimal length (too little context vs long sentence)
        lenDiff = max( 0, min( 9, abs( C('optimal sentence length') - N ) -2 ) )
        tooLong = N > C('optimal sentence length')

            # calculate mmi
        mmi = 10000*N_k + 1000*lenDiff + usefulness
        if C('set due based on mmi'):
            nid2mmi[ nid ] = mmi

        # Fill in various fields/tags on the note based on cfg
        ts, fs = TAG.split( tags ), splitFields( flds )
            # determine card type
        compTag, vocabTag, notReadyTag, alreadyKnownTag, priorityTag, badLengthTag, tooLongTag = tagNames = C('tag_comprehension'), C('tag_vocab'), C('tag_notReady'), C('tag_alreadyKnown'), C('tag_priority'), C('tag_badLength'), C('tag_tooLong')
        if N_m == 0:    # sentence comprehension card, m+0
            ts = [ compTag ] + [ t for t in ts if t not in [ vocabTag, notReadyTag ] ]
            setField( mid, fs, C('focusMorph'), u'' )
        elif N_k == 1:  # new vocab card, k+1
            ts = [ vocabTag ] + [ t for t in ts if t not in [ compTag, notReadyTag ] ]
            setField( mid, fs, C('focusMorph'), u'%s' % focusMorph.base )
        elif N_k > 1:   # M+1+ and K+2+
            ts = [ notReadyTag ] + [ t for t in ts if t not in [ compTag, vocabTag ] ]

            # set type agnostic fields
        setField( mid, fs, C('k+N'), u'%d' % N_k )
        setField( mid, fs, C('m+N'), u'%d' % N_m )
        setField( mid, fs, C('morphManIndex'), u'%d' % mmi )
        setField( mid, fs, C('unknowns'), u', '.join( u.base for u in unknowns ) )
        setField( mid, fs, C('unmatures'), u', '.join( u.base for u in unmatures ) )
        setField( mid, fs, C('unknownFreq'), u'%d' % F_k_avg )

            # other tags
        if priorityTag in ts:   ts.remove( priorityTag )
        if isPriority:          ts.append( priorityTag )

        if badLengthTag in ts:  ts.remove( badLengthTag )
        if lenDiff:             ts.append( badLengthTag )

        if tooLongTag in ts:    ts.remove( tooLongTag )
        if tooLong:             ts.append( tooLongTag )

            # update sql db
        tags_ = TAG.join( TAG.canonify( ts ) )
        flds_ = joinFields( fs )
        if flds != flds_ or tags != tags_:  # only update notes that have changed
            csum = fieldChecksum( fs[0] )
            sfld = stripHTML( fs[ getSortFieldIndex( mid ) ] )
            ds.append( { 'now':now, 'tags':tags_, 'flds':flds_, 'sfld':sfld, 'csum':csum, 'usn':mw.col.usn(), 'nid':nid } )

    mw.progress.update( value=i, label='Updating anki database...' )
    mw.col.db.executemany( 'update notes set tags=:tags, flds=:flds, sfld=:sfld, csum=:csum, mod=:now, usn=:usn where id=:nid', ds )
    TAG.register( tagNames )

    # Now reorder new cards based on MMI
    mw.progress.update( value=i, label='Updating new card ordering...' )
    ds = []
    for ( cid, nid, due ) in db.execute( 'select id, nid, due from cards where type = 0' ):
        if nid in nid2mmi: # owise it was disabled
            due_ = nid2mmi[ nid ]
            if due != due_: # only update cards that have changed
                ds.append( { 'now':now, 'due':due_, 'usn':mw.col.usn(), 'cid':cid } )
    mw.col.db.executemany( 'update cards set due=:due, mod=:now, usn=:usn where id=:cid', ds )
    mw.reset()

    printf( 'Updated notes in %f sec' % ( time.time() - t_0 ) )
    mw.progress.finish()
    return knownDb

def main():
    # load existing all.db
    mw.progress.start( label='Loading existing all.db', immediate=True )
    t_0 = time.time()
    cur = util.allDb() if cfg1('loadAllDb') else None
    printf( 'Loaded all.db in %f sec' % ( time.time() - t_0 ) )
    mw.progress.finish()

    # update all.db
    allDb = mkAllDb( cur )

    # merge in external.db
    mw.progress.start( label='Merging ext.db', immediate=True )
    ext = MorphDb( cfg1('path_ext'), ignoreErrors=True )
    allDb.merge( ext )
    mw.progress.finish()

    # update notes
    knownDb = updateNotes( allDb )
    
    # update stats and refresh display
    stats.updateStats( knownDb )
    mw.toolbar.draw()

    # set global allDb
    util._allDb = allDb
