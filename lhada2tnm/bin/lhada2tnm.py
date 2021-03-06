#!/usr/bin/python
#--------------------------------------------------------------------------------
# Description: Prototype translator of a LHADA description to a TNM analyzer
# Created: 12-Dec-2017 Harrison B. Prosper & Sezen Sekmen
#          12-May-2018 HBP assume case insensitivity, remove dependence on loop
#                      keyword, better handling of mapping from vector<TEParticle>
#                      to vector<TLorentzVector>
#          14-May-2018 HBP add count histogram for each cut block
#          16-May-2018 HBP completely decouple lhada analyzer from tnm
#          18-May-2018 HBP fix bug process_functions
#          14-Oct-2018 HBP use LHADA2TNM_PATH/external/include to find includes
#          20-Mar-2019 HBP fix implicit loop bug and make implicit loops more
#                      robust
#--------------------------------------------------------------------------------
import sys, os, re, optparse, urllib
from time import ctime
from string import joinfields, split, replace, find, strip, lower, rstrip
#--------------------------------------------------------------------------------
VERSION = 'v1.0.3'

DEBUG = 0

# ADL block types
BLOCKTYPES = ['info', 'table', 'function', 'object', 'variable', 'cut']

# ADL keywords
KEYWORDS   = ['experiment',
              'id',
              'publication',
              'sqrtS',
              'lumi',
              'arXiv',
              'hepdata',
              'doi',
              'arg',
              'code',
              'take', 'select', 'apply', 'reject']
    
TOKENS = set(BLOCKTYPES+KEYWORDS)

SPACE6   = ' '*6

# some simple regular expression to dissect ADL file
KWORDS   = r'\b(%s)\b' % joinfields(KEYWORDS, '|')
exclude  = re.compile(KWORDS)
getwords = re.compile('lha[.][a-zA-Z0-9]+|[a-zA-Z][a-zA-Z0-9_]*')
getvars  = re.compile('@?[a-zA-Z][a-zA-Z0-9_.;:]*@?')
getdvars = re.compile('[a-zA-Z]+[a-zA-Z0-9;:]*[.]')

cppAND   = re.compile(r'\band|AND\b')
cppOR    = re.compile(r'\bor|OR\b')
cppEQEQ  = re.compile('(?<!=|[>]|[<])=')
scrubdot = re.compile('[a-zA-Z]+[.]')
getfunctions = re.compile('^\s*[\w_]+\s+[a-zA-Z][\w_]+\s*[(][^{]+', re.M)
tlorentz_vector = re.compile('vector\s*[<]\s*TLorentzVector\s*[>]')
nip      = re.compile('[_](?=[a-zA-Z])|(?<=[a-zA-Z0-9])[_](?= )')

# some objects are singletons, that is, there is only one instance of the
# object per event. try to guess which ones:
# this algorithm is very simpleminded; will have to do better later
single   = re.compile('missing|met|event|scalar')
#--------------------------------------------------------------------------------
NAMES = {'name': 'analyzer',
             'treename': 'Delphes',
             'info': '//',
             'time': ctime(),
             'aoddef': '',
             'aodimpl': '',
             'adapter': 'adapter',
             'analyzer': 'analyzer',
             'version': VERSION
             }
    
SINGLETON_CACHE = set()

# C++ ADL analyzer template
TEMPLATE_CC =\
'''//------------------------------------------------------------------
// File:        %(name)s_s.cc
// Description: Analyzer for ADL-based analysis:
%(info)s
// Created:     %(time)s by lhada2tnm.py %(version)s
//------------------------------------------------------------------
#include <algorithm>
#include "%(name)s_s.h"
%(includes)s
using namespace std;
//------------------------------------------------------------------
// The following functions, objects, and variables are globally visible
// within this programming unit.
//------------------------------------------------------------------
%(fundef)s
//------------------------------------------------------------------
%(vardef)s
//------------------------------------------------------------------
%(objdef)s
//------------------------------------------------------------------
%(cutdef)s
//------------------------------------------------------------------
%(name)s_s::%(name)s_s()
{
%(vobjects)s
%(vcuts)s }

%(name)s_s::~%(name)s_s() {}

%(runargsimpl)s
{
  // copy to internal buffers
%(copyargsimpl)s
  // create filtered objects
  for(size_t c=0; c < objects.size(); c++) objects[c]->create();
    
%(varimpl)s
  // apply event level selections
  for(size_t c=0; c < cuts.size(); c++)
    { 
      cuts[c]->reset();
      cuts[c]->apply();
    }
}

void %(name)s_s::summary(TFile* fout, ostream& os)
{
  os << std::endl << "Summary" << std::endl << std::endl;
  for(size_t c=0; c < cuts.size(); c++)
    {
      cuts[c]->summary(os);
      cuts[c]->write(fout);
    }
}
'''

# C++ ADL analyzer header template
TEMPLATE_HH =\
'''#ifndef %(name)s_s_HH
#define %(name)s_s_HH
//------------------------------------------------------------------
// File:        %(name)s_s.h
// Description: Analyzer for ADL-based analysis:
%(info)s
// Created:     %(time)s by lhada2tnm.py %(version)s
//------------------------------------------------------------------
#include <algorithm>
#include <iostream>
#include "TFile.h"
#include "TH1F.h"
#include "TEParticle.h"
//------------------------------------------------------------------
struct lhadaThing
{
  lhadaThing() {}
  virtual ~lhadaThing() {}
  virtual void reset() {}
  virtual void create() {}
  virtual bool apply() { return true; }
  virtual void write(TFile* fout) {}
  virtual void summary(std::ostream& os) {}
};
    
struct %(name)s_s
{
  std::vector<lhadaThing*> objects;
  std::vector<lhadaThing*> cuts;

  %(name)s_s();
  ~%(name)s_s();
  void run(%(runargs)s);
  void summary(TFile* fout, std::ostream& os);
};
#endif
'''

# C++ TNM analyzer template
TNM_TEMPLATE_CC =\
'''//------------------------------------------------------------------
// File:        %(name)s.cc
// Description: Analyzer for ADL analysis:
%(info)s
// Created:     %(time)s by lhada2tnm.py %(version)s
//------------------------------------------------------------------
#include "tnm.h"
#include "%(adaptername)s.h"
#include "%(name)s_s.h"

using namespace std;
//------------------------------------------------------------------
int main(int argc, char** argv)
{
  // If you want canvases to be visible during program execution, just
  // uncomment the line below
  //TApplication app("%(name)s", &argc, argv);

  // Get command line arguments
  commandLine cl(argc, argv);
    
  // Get names of ntuple files to be processed
  vector<string> filenames = fileNames(cl.filelist);

  // Create tree reader
  itreestream stream(filenames, "%(treename)s");
  if ( !stream.good() ) error("can't read root input files");

  // Create a buffer to receive events from the stream
  // The default is to select all branches
  // Use second argument to select specific branches
  // Example:
  //   varlist = 'Jet_PT Jet_Eta Jet_Phi'
  //   ev = eventBuffer(stream, varlist)

  eventBuffer ev(stream);
  int nevents = ev.size();
  cout << "number of events: " << nevents << endl;

  // Create output file for histograms; see notes in header 
  outputFile of(cl.outputfilename);
  //------------------------------------------------------------------
  // Define histograms
  //------------------------------------------------------------------
  //setStyle();

  //------------------------------------------------------------------
  // Create an event adapter to map input types to a standard internal 
  // type and create the analyzer
  //------------------------------------------------------------------
  %(adaptername)s %(adapter)s;

  %(name)s_s %(analyzer)s;
  //------------------------------------------------------------------
  // Loop over events
  //------------------------------------------------------------------
  for(int entry=0; entry < nevents; entry++)
    {
      // read an event into event buffer
      ev.read(entry);

      if ( entry %(percent)s 10000 == 0 ) cout << entry << endl;

%(extobjimpl)s
%(runimpl)s
    }

  // summarize analysis
  %(analyzer)s.summary(of.file_, cout);

  ev.close();
  of.close();
  return 0;
}
'''
#--------------------------------------------------------------------------------
USAGE ='''
    Usage:
       lhada2tnm.py [options] ADL-file-name

    Options:
    -a name of analyzer to be created [analyzer]
    -e name of event adapter          [DelphesAdapter]
    -t name of ROOT tree              [Delphes]

    Available adapters       tree name
    ----------------------------------
    DelphesAdapater          Delphes
    CMSNanoAODAdapter        Events
    '''
def decodeCommandLine():

    parser = optparse.OptionParser(usage=USAGE, version=VERSION)
            
    parser.add_option("-a", "--analyzer",
                      action="store",
                      dest="name",
                      type="string",
                      default=NAMES['analyzer'],
                      help="name of analyzer to be created")

    parser.add_option("-e", "--eventadapter",
                      action="store",
                      dest="adaptername",
                      type="string",
                      default='DelphesAdapter',
                      help="name of event adapter")

    parser.add_option("-t", "--tree",
                      action="store",
                      dest="treename",
                      type="string",
                      default='Delphes',
                      help="name of ROOT tree")

    options, args = parser.parse_args()
    if len(args) == 0:
        sys.exit(USAGE)

    # make sure correct tree goes with specified event adapter if treename
    # is not given
    if options.adaptername == 'CMSNanoAODAdapter':
        if options.treename == 'Delphes':
            options.treename = 'Events'
            
    filename = args[0]
    print('''
    analyzer:        %(name)s
    event adapter:   %(adaptername)s
    ROOT tree:       %(treename)s
    ADL filename:    %(filename)s
''' % {'name': options.name,
           'adaptername': options.adaptername,
           'treename': options.treename,
           'filename': filename})

    return (filename, options)

def nameonly(s):
    return posixpath.splitext(posixpath.split(s)[1])[0]

def join(left, a, right):
    s = ""
    for x in a:
        s = s + "%s%s%s" % (left, x, right)
    return s

# split a record into words, but exclude ADL keywords
def getWords(records):
    words = []
    for record in records:
        words += split(record)
    # exclude ADL keywords
    record = joinfields(words, ' ')
    words  = split(exclude.sub('', record))
    return words

def boohoo(message):
    sys.exit('** lhada2tnm.py * %s' % message)
#------------------------------------------------------------------------------
# Look for header file on given list of search paths
#------------------------------------------------------------------------------
def findHeaderFile(infile, incs):
	ifile = strip(os.popen("find %s 2>/dev/null" % infile).readline())    
	if ifile != "":
		filepath = ifile
		for includepath in incs:
			tmp = splitfields(filepath, includepath + '/',1)
			if len(tmp) == 2:
				base, ifile = tmp
			else:
				base = ''
				ifile = tmp[0]

			if base == '': return (filepath, ifile)
		return (filepath, filepath)

	ifile = infile
	for includepath in incs:
		filepath = includepath + "/" + ifile
		filepath = strip(os.popen("find %s 2>/dev/null" % filepath).readline())
		if filepath != "": return (filepath, ifile)

		filepath = includepath + "/include/" + ifile
		filepath = strip(os.popen("find %s 2>/dev/null" % filepath).readline())
		if filepath != "": return (filepath, ifile)

	return ("","")
#------------------------------------------------------------------------------
# Make a valiant attempt to analyze, and extract, calling sequence of a function
#------------------------------------------------------------------------------
funcname = re.compile('[a-zA-Z]+[\w\<:,\>]*(?=[(])')
findtemparg = re.compile('(?<=[<]).+(?=[>])')
def decodeFunction(record):
    record = replace(record, "\n", " ")
    fname  = funcname.findall(record)
    if len(fname) == 0:
        boohoo("can't decode %s" % record)
    fname = fname[0]
    rtype, args = split(record, fname)
    rtype = strip(rtype)
    args  = strip(args)[1:-1]
    
    # since template arguments can include commas, hide them before
    # splitting using commas
    targs  = findtemparg.findall(args)
    hidden = []
    for ii, t in enumerate(targs):
        orig = '<%s>' % t
        temp = '@%d' % ii
        hidden.append((orig, temp))
        args = replace(args, orig, temp)

    # replace commas by "#" and go back to original types
    # and split at "#"
    args = map(strip, split(args, ","))
    args = joinfields(args, '#')
    for orig, temp in hidden:
        args = replace(args, temp, orig)

    # ok, now get types etc.
    args = map(strip, split(args, "#"))
    argtypes = []
    argnames = []
    for arg in args:
        t = split(arg, ' ')
        if len(t) == 1:
            argtypes.append(t[0])
            argnames.append('')
        elif len(t) > 1:
            argtypes.append(joinfields(t[:-1], ' '))
            argnames.append(t[-1])
    return (rtype, fname, argtypes, argnames)
#-----------------------------------------------------------------------------
# Use a simple bubble-like sort to sort blocks according to dependency
#------------------------------------------------------------------------------
def sortObjects(objects):
    from copy import deepcopy
    # 1) get blocks with no dependencies on internal blocks
    obs = []
    names = []
    for t in objects:
        name  = t[0]
        words = t[1]
        if len(words) > 0: continue
        # this block has no internal dependencies
        obs.append(deepcopy(t))
        names.append(name)

        t[0] = None
    names = set(names)
    
    # 2) sort remaining blocks so that dependent blocks occur after the
    #    the blocks on which they depend
    for ii in range(len(objects)):
        for t in objects:
            name  = t[0]
            if name == None: continue
            words = t[1]                
            
            incomplete = len(words.intersection(names)) < len(words) 
            if incomplete: continue

            # the dependency of this block has been fully satisfied
            obs.append(deepcopy(t))
            names.add(name)
            t[0] = None
    return obs    
#--------------------------------------------------------------------------------
# Read ADL file and extract blocks into a simple internal data structure
#--------------------------------------------------------------------------------
def extractBlocks(filename):
    if DEBUG > 0:
        print '\nBEGIN( extractBlocks )'
        
    from copy import deepcopy
    import re
    from string import rstrip, split, strip    
    #--------------------------------------------
    # read ADL file
    #--------------------------------------------    
    try:
        records = open(filename).readlines()
    except:
        boohoo('unable to open ADL file %s' % filename)

    #--------------------------------------------    
    # set up some simple regular expressions
    #--------------------------------------------    
    stripcomments = re.compile('[#].*')
    blocktypes = r'\b(%s)\b' % joinfields(BLOCKTYPES, '|')
    isblock    = re.compile(blocktypes)
    fcallname  = re.compile('[a-zA-Z_][\w_]*\s*(?=[(])')
    
    #--------------------------------------------    
    # strip out comments and blank lines,
    # but note original line numbers of records
    #--------------------------------------------        
    orig_records = [x for x in records]
    records = []
    for ii, record in enumerate(orig_records):
        lineno = ii+1
        # strip away comments from current record
        record = stripcomments.sub('', record)
        record = strip(record)
        if record == '': continue
        records.append((lineno, record))

    #--------------------------------------------    
    # loop over ADL records
    #--------------------------------------------    
    blocks = {}
    bname  = None
    funnames    = [] # keep track pf declared function names
    varnames    = [] # keep track of declared variables
    objectnames = [] # keep track of block names
    cutnames    = [] # keep track of cut names
    statement   = [] # keep track of records that comprise a statement
    
    for ii, (lineno, record) in enumerate(records):
        
        # look for a block
        if isblock.findall(record) != []:

            # found a block; get its type and name
            t = split(record)
            if len(t) != 2:
                boohoo('problem at line\n%4d %s\n' % (lineno, record))
            # block type, block name
            btype, bname = t

            # modify internal function and variable names in an
            # attempt to avoid name collisions
            if btype == 'function':
                bname = '_%s' % bname
            elif btype == 'variable':
                bname = '%s_' % bname

            # fall on sword if we have duplicate block names
            if blocks.has_key(bname):
                boohoo('duplicate block name %s at line'\
                           '\n%4d %s\n' % (bname, lineno, record))
                            
            blocks[bname] = {'type': btype, 'body': []}
            if btype == 'object':
                objectnames.append(bname)
            elif btype == 'cut':
                cutnames.append(bname)
            elif btype == 'function':
                funnames.append(bname)
            elif btype == 'variable':
                varnames.append(bname)
            continue
        
        if bname == None:
            boohoo('problem at line\n%4d %s\n' % (lineno, record))

        # statements can extend over multiple records (lines), so
        # lookahead to determine if current record is the end
        # of the current statement
        statement.append(record)
        if ii < len(records)-1:
            next_lineno, next_record = records[ii+1]
            next_token = split(next_record)[0]
            
            # check next token
            if  next_token in TOKENS:
                # next token is a reserved token (either a blocktype
                # or a keyword), so current record is the end of the
                # current statement
                blocks[bname]['body'].append(joinfields(statement, ' '))
                # remember to reset statement
                statement = []
        else:
            # reached end of records
            blocks[bname]['body'].append(joinfields(statement, ' '))
            statement = []
            
    # convert to sets for easier comparisons
    objectnames = set(objectnames)
    cutnames    = set(cutnames)
    varnames    = set(varnames)
    funnames    = set(funnames)
    
    # reorganize the blocks
    blockmap = {}
    for key in blocks.keys():
        block = blocks[key]
        blocktype = block['type']
        if not blockmap.has_key(blocktype):
            blockmap[blocktype] = []
        body  = block['body']
        words = joinfields(getWords(body), ' ')
        words = set(getwords.findall(words))

        # if block type is object, strip away words within this
        # block that are not object names using set intersection:
        # C = A and B. the remaining words will be user-defined
        # words and will be used to sort the blocks according to
        # block-to-block dependency
        if blocktype == 'object':
            words = objectnames.intersection(words)
        elif blocktype == 'cut':
            words = cutnames.intersection(words)
            
        blockmap[blocktype].append([key, deepcopy(words), deepcopy(body)])

    # for object and cut blocks modify names of internal variables
    # and functions in order to avoid name collisions
    for fname in funnames:
        # skip "_"
        edit = re.compile('\\b%s\\b\s*(?=[(])' % fname[1:])
        
        for btype in ['object', 'cut']:
            for jj, (bname, words, records) in enumerate(blockmap[btype]):
                for ii, record in enumerate(records):
                    records[ii] = edit.sub(fname, record)
                    if DEBUG > 1:
                        print "fun-change( %s ) -> ( %s )" \
                          % (strip(record), strip(records[ii]))
                blockmap[btype][jj][-1] = records
                
    for vname in varnames:
        # skip "_"
        edit = re.compile('\\b%s\\b(?!=[(])' % vname[:-1])
        
        for btype in ['object', 'cut']:
            for jj, (bname, words, records) in enumerate(blockmap[btype]):
                for ii, record in enumerate(records):
                    records[ii] = edit.sub(vname, record)
                    if DEBUG > 1:
                        print "var-change( %s ) -> ( %s )" \
                          % (strip(record), strip(records[ii]))
                blockmap[btype][jj][-1] = records
                
    # sort object blocks so that a block that depends on other blocks
    # is placed after those blocks.
    if blockmap.has_key('object'):
        blockmap['object'] = sortObjects(blockmap['object'])

    # sort cut blocks so that a block that depends on other blocks
    # is placed after those blocks.
    if blockmap.has_key('cut'):
        blockmap['cut'] = sortObjects(blockmap['cut'])

    return blockmap
#--------------------------------------------------------------------------------
def printBlocks(blocks):
    out = open('blocks.log', 'w')
    for blocktype in BLOCKTYPES:
        if not blocks.has_key(blocktype): continue
        record = "[%s]" % blocktype
        out.write('%s\n' % record)
        record = '-'*80
        out.write('%s\n' % record)
        for name, words, body in blocks[blocktype]:
            record = "%-12s\t%s" % (name, words)
            out.write('%s\n' % record)
            for record in body:
                record = "\t%s" % record
                out.write('%s\n' % record)
            record = '-'*80
            out.write('%s\n' % record)
    out.close()
#--------------------------------------------------------------------------------
# The following functions convert ADL blocks to C++
#--------------------------------------------------------------------------------
def process_info(names, blocks):
    info = '//\n// LHADA file: %(filename)s\n' % names
    info+= '// info block\n'

    if not blocks.has_key('info'):
        boohoo("Thou lump of foul deformity. I can't find info block!")

    name, words, body = blocks['info'][0]
    
    for record in body:
        t = split(record)
        record = '//\t%-12s\t%s\n' % (t[0], joinfields(t[1:], ' '))
        info += record
    info += '//'
    names['info'] = info
#--------------------------------------------------------------------------------
def process_functions(names, blocks):
    if DEBUG > 0:
        print '\nBEGIN( process_function )'

    if not blocks.has_key('function'): return

    # extract headers to be included
    includeset = set()
    for name, words, records in blocks['function']:
        for record in records:
            t = split(record)
            token = t[0]
            if token != 'code': continue
            includeset.add(t[1])
            if DEBUG > 1:
                print "  FUNCTION( %s) CODE( %s )" % (name, t[1])
                
    includes = ''
    for record in includeset:
        includes += '#include "%s"\n' % record
    names['includes'] = includes

    # create functions
    fundef = '//\n// functions\n'
    functions = {}
    for name, words, records in blocks['function']:
        
        # NB: remove "_" from name, which was added in extractBlocks
        name = name[1:]

        # cache original name
        origname = name
        
        # this function could be within a namespace:
        # <namespace>.[<namespace>.]<function>
        t = split(name, '.')
        name = t[-1] # assume function name is last field
        if len(t) > 1:
            namespace = joinfields(t[:-1], '::') + '::'
        else:
            namespace = ''
        extname = namespace + name
        # prefix internal name with an "_"
        intname = '_%s' % replace(extname, '::', '_')

        if DEBUG > 0:
            print "  internal, external names( %s, %s )" % (intname, extname)
            
        # get details of external function
        args = []
        for record in records:
            t = split(record)
            token = t[0]
            if token == 'arg':
                args.append(t[1])
                continue
            elif token != 'code':
                continue

            # got code. could be inline or external (via a header)
            code  = t[1]
            # internal code not yet implemented
            if code == 'c++': continue
                
            # assume code declaration is within a header. find header
            code = strip(code)
            t = findHeaderFile(code, ['$LHADA2TNM_PATH/external/include'])
            if len(t) == 0:
                boohoo('unable to locate header: %s' % code)

            # header found, scan it to get details of functions
            include = t[0]
            if include == '':
                boohoo('problem getting header name: %s' % code)

            # but first copy header to local include directory
            os.system('cp %s include/' % include)
                          
            record = open(include).read()
            
            # find all function declarations in header
            t = getfunctions.findall(record)
            for x in t:
                
                # make a valiant attempt to decode function
                rtype, namen, argtypes, argnames = decodeFunction(x)
                
                if name == namen:
                    
                    # we have a match, so create internal function
                    if len(args) != len(argtypes):
                        boohoo('''argument count mismatch in function %s.
expected arguments %s, but %s found in ADL file
''' % (origname, v, args))

                    # note: internal functions could have arguments
                    # such as vector<TEParticle> that map to
                    # vector<TLorentzVector>. unfortunately, even though
                    # TEParticle inherits from TLorentzVector, vector<TEParticle>
                    # is not type compatible with vector<TLorentzVector>. So,
                    # we need to copy each TEParticle to a TLorentzVector.
                    # however, a singleton TEParticle is type compatible with
                    # TLorentzVector and no copying is needed.
                    copyvars=''
                    argsrec = ''
                    argscall= ''
                    for ii, arg in enumerate(args):
                        argc = arg
                        # check for vector<TLorenzVector>
                        if tlorentz_vector.findall(argtypes[ii]) != []:
                            argc = arg + '_'
                            copyvars+='\n'
                            copyvars+='  vector<TLorentzVector> %s(%s.size());\n'\
                              % (argc, arg)
                            copyvars+='  copy(%s.begin(), %s.end(), %s.begin());'\
                              % (arg, arg, argc)
                            argtypes[ii] = 'vector<TEParticle>&'
                            
                        argsrec += '%s %s, ' % (argtypes[ii], arg)
                        argscall+= '%s, ' % argc
                    if argsrec != '':
                        argsrec = argsrec[:-2]
                        argscall= argscall[:-2]
                    
                    fundef += '''inline\n%(rtype)s\t%(intname)s(%(args)s)
{%(copyvars)s
  return %(extname)s(%(argscall)s);
};

''' % {'rtype': rtype,
           'intname': intname,
           'extname': extname,
           'copyvars': copyvars,
           'args': argsrec,
           'argscall': argscall}
                    # cache function info
                    functions[origname] = (rtype, intname, extname, argtypes)
                    
                    if DEBUG > 0:
                        print '  function details( %s ); %s' % (origname,
                                                          functions[origname])
    names['fundef'] = fundef
    
    blocks['function_info'] = functions
#--------------------------------------------------------------------------------
# check whether we have at least one implicit loop in the current record.
# we have an implicit loop if the next record contains a variable
# of the form <objectname>.<variable> and objectname is not
# a singleton. here a singleton is defined to be an object of which only one
# occurs per event. for now we do not handle nested implicit loops
#--------------------------------------------------------------------------------
def checkForImplicitLoops(record, blocktypes):
    loopables = []
        
    # get words from record, including those of the form <name>.<variable>
    words   = set(getvars.findall(record))
    if DEBUG > 0:
        print "checkForImplicitLoops( %s )" % words

    # identify words of the form <name>.<variable> and check if the word
    # <name> appears in the list of non-singleton objects. if it does, then we
    # assume that we are to loop over name and access one of its attributes
    objectnames = set(blocktypes['object'])

    for x in words:
        t = split(x, '.')
        if len(t) > 1:
            name = t[0]
            if name in objectnames:
                if name in SINGLETON_CACHE:
                    if DEBUG > 0:
                        print "\tfound singleton object( %s )" % name
                    continue
                else:
                    if DEBUG > 0:
                        print "\tfound implicit loop over object( %s )" % name
                    loopables.append(name)
    return loopables
#--------------------------------------------------------------------------------
# handle cutvectors depending on whether we have a select or a reject
#--------------------------------------------------------------------------------
def fixrecord(record):
    # start with some simple replacements
    record = replace(record, "|", "@")
    record = replace(record, "[", ";:")
    record = replace(record, "]", ":;")
    # replace AND and OR with c++ syntax for the same
    record = cppAND.sub('&&', record)
    record = cppOR.sub('||\n\t', record)
    # use a set to avoid recursive edits
    words  = set(getvars.findall(record))
    if DEBUG > 0:
        print "RECORD( %s )" % record
        print "\tWORDS( %s )" % words
    return (record, words)

def setlogic(record, tab, cutvector, logic_op):
    t = fixrecord(record)
    words = t[-1]
    rec = ''
    for name in words:    
        if name in cutvector:
            rec += '%s%s.logical(%s);\n' % (tab, name, logic_op)
    return rec
#--------------------------------------------------------------------------------
# convert given ADL record into the corresponding C++ code snippet
# record:     current ADL record
# btype:      current ADL block type or apply
# blocktypes: block types and associated names
#--------------------------------------------------------------------------------
def convert2cpp(record, btype, blocktypes, cutvector=set()):
    record, words = fixrecord(record)
        
    for name in words:
        # if this is an object block,
        # check if variable is of the form a.b
        # if it is, we make the replacement a.b -> a("b"). 
        # but, consider PT and e.PT,
        # we want
        #    PT    -> p("pt") if PT is not preceded by "e."
        #    e.PT  -> e("pt") if PT is preceded by "e."
        # if, however, this is a cut object, we make the following
        # changes: .size -> .size()
        #          a.b   -> a("b")
        # however, if a variable is a local variable, e.g., assigned
        # within an implied loop, it should be used as is.
        
        t = split(name, '.')
        undotted = len(t) == 1
        oname = t[0]

        # check for singleton
        a_singleton = oname in SINGLETON_CACHE
        
        field     = t[-1]
        newfield  = field
        oldrecord = record
        prerecord = ''
        
        # need to handle things like PT and e.PT
        # also need to check for singleton objects
        if btype == 'object':
            if name in cutvector:
                pass # use name as is
            elif undotted:
                if not a_singleton: oname = "p"
                edit = re.compile('(?<![.])%s' % field)
                newfield = '%s("%s")' % (oname, lower(field))
                record = edit.sub(newfield, record)
            else:
                edit = re.compile('\\b%s\\b' % name)
                newfield = '%s("%s")' % (oname, lower(field))
                record = edit.sub(newfield, record)                

            if DEBUG > 0:
                print "\tobject: oname( %s ) field( %s ) newfield( %s )" % \
                (oname, field, newfield)
                print "\t\told-record( %s )" % strip(oldrecord)
                print "\t\tnew-record( %s )" % strip(record)
            
        elif btype == 'apply':
            if undotted:
                if not a_singleton: oname = "p"
                edit = re.compile('(?<![.])%s' % field)
                newfield = '%s("%s")' % (oname, lower(field))
                record = edit.sub(newfield, record)                
            else:
                if not a_singleton: oname = "q"
                edit = re.compile('\\b%s\\b' % name)
                newfield = '%s("%s")' % (oname, lower(field))
                record = edit.sub(newfield, record)                
            
            if DEBUG > 0:
                print "\tapply: oname( %s ) field( %s ) newfield( %s )" % \
                (oname, field, newfield)
                print "\t\told-record( %s )" % strip(oldrecord)
                print "\t\tnew-record( %s )" % strip(record)               
            
        elif btype == 'cut':
            if undotted:
                # check if this is the result of another block
                if name in blocktypes['cut']:
                    edit = re.compile('\\b%s\\b' % name)
                    newfield = 'cut_%s()' % name
                    if DEBUG > 0:
                        print "\tcut: oname( %s ) field( %s ) newfield( %s )" % \
                          (oname, field, newfield)
                          
                    record = edit.sub(newfield, record)

            else:
                edit = re.compile('\\b%s\\b' % name)
                if field == 'size':
                    newfield = '%s.%s()' % (oname, field)
                else:
                    newfield = '%s("%s")' % (oname, lower(field))
                    
                if DEBUG > 0:
                    print "\tapply: oname( %s ) field( %s ) newfield( %s )" % \
                    (oname, field, newfield)
                    print "\t\told-record( %s )" % strip(oldrecord)
                
                record = edit.sub(newfield, record)
        
            if DEBUG > 0:
                print "\t\told-record( %s )" % strip(oldrecord)                    
                print "\t\tnew-record( %s )" % strip(record)                    

    # now go back to original symbols |, [, and ]    
    record = replace(record, '@',  '|')
    record = replace(record, ';:', '[')
    record = replace(record, ':;', ']')
    
    if DEBUG > 0:
        print "\t\tcleaned-record( %s )" % record
        print '-'*80
    return record

def process_singleton_object(name, records, tab, blocktypes):
    if DEBUG > 0:
        print '\nBEGIN( process_singleton_object ) %s' % name
                    
    objdef = ''
    for record in records:
        t = split(record)
        token = t[0]
        value = joinfields(t[1:], ' ')
        if   token == 'take':
            objdef += '%s%s = %s;\n' % (tab, name, value);
    return objdef

def process_multiple_objects(name, records, TAB, blocktypes):
    if DEBUG > 0:
        print '\nBEGIN( process_multiple_objects ) %s' % name
            
    tab     = TAB
    tab4    = ' '*4
    objdef  = '%s%s.clear();\n' % (tab, name)

    # cache for names of returned vector-valued variables
    # associated with loopable objects
    cutvector = set() 
    
    if DEBUG > 0:
        print "\nNAME( %s )" % name
    
    for index in xrange(len(records)):
        record= records[index]
        t     = split(record)
        token = t[0]
        value = joinfields(t[1:], ' ')

        if DEBUG > 0:
            print "TOKEN( %s )\tvalue( %s )" % (token, value)

        # check for implicit loops in current statement
        loopables = checkForImplicitLoops(record, blocktypes)

        if   token == 'take':
            # --------------------------------------------            
            # TAKE
            # --------------------------------------------            
            objdef += '%sfor(size_t c=0; c < %s.size(); c++)\n' % (tab, value)
            objdef += '%s  {\n' % tab
            objdef += '%s%sTEParticle& p = %s[c];\n' % (tab, tab4, value)
            
        elif token == 'apply':
            # --------------------------------------------            
            # APPLY
            # --------------------------------------------
            # get function call and function name
            # check that function has been declared
            fcall  = joinfields(t[1:-1], ' ')
            if DEBUG > 0:
                print "\tfunction call( %s )" % fcall
                
            fname = strip(split(fcall, '(')[0])
            function_found = False
            for fnamen in blocktypes['function']:
                if fname == fnamen:
                    if DEBUG > 0:
                        print "\tfunction found( %s )" % fname
                    function_found = True
                    break
            if not function_found:
                boohoo('please use a function block to declare function %s' % fname)

            rvalue_name = t[-1]  # name of return value
            
            if fcall[-1] != ')':
                boohoo('''
%s
perhaps you're missing a return value in:
%s
''' % (objdef, record))

            a, b = split(fcall, '(')
            b = convert2cpp(b, 'apply', blocktypes)
            a = replace(a, '.', '_')
            fcall = '%s(%s' % (a, b)   # function call
            
            if loopables != []:
                # this function call contains an implicit loop and
                # therefore returns multiple values that we refer to as a
                # cutvector. we, therefore, need to cache the name of the
                # cutvector because it is surely used in a subsequent
                # select or reject statement.
                cutvector.add(rvalue_name)

                # for now, we handle function calls with a single
                # implicit loop.
                object_name = loopables[0]
                
                # adjust tab
                tab = TAB + tab4

                if DEBUG > 0:
                    print '   BEGIN( IMPLICIT LOOP )'
                    print '     %s' % fcall
                
                objdef += '%scutvector<double> %s(%s.size());\n' % (tab,
                                                                    rvalue_name,
                                                                    object_name)
                objdef += '%sfor(size_t n=0; n < %s.size(); n++)\n' % \
                (tab, object_name)
                objdef += '%s  {\n' % tab
                objdef += '%s%sTEParticle& q = %s[n];\n' % (tab, tab4, object_name)
                objdef += '%s%s%s[n] = %s;\n' % (tab, tab4, rvalue_name, fcall)
                objdef += '%s  }\n' % tab
                if DEBUG > 0:
                    print '   END( IMPLICIT LOOP )'

                # reset tab
                tab = TAB

        elif token == 'select':
            # --------------------------------------------            
            # SELECT
            # --------------------------------------------
            # if the current record contains a cutvector variable, then
            # add a statement to specify whether we should AND or OR the
            # truth values associated with the cut on each element of the
            # cutvector. We assume that a select requires every cut be
            # true, while a reject requires at least one cut be true.
            objdef += setlogic(value, tab+tab4, cutvector, 'AND')
            
            objdef += '%s%sif ( !(%s) ) continue;\n' % \
              (tab, tab4, convert2cpp(value, 'object', blocktypes, cutvector))

        elif token == 'reject':
            # --------------------------------------------            
            # REJECT
            # --------------------------------------------
            objdef += setlogic(value, tab+tab4, cutvector, 'OR')
            
            objdef += '%s%sif ( %s ) continue;\n' % \
              (tab, tab4, convert2cpp(value, 'object', blocktypes, cutvector))
            
    objdef += '%s%s%s.push_back(p);\n' % (tab, tab4, name)
    objdef += '%s  }\n' % tab
    return objdef

def process_objects(names, blocks, blocktypes):
    if DEBUG > 0:
        print '\nBEGIN( process_objects )'

    if not blocks.has_key('object'): return  ''
        
    from string import lower
    tab2 = ' '*2
    tab4 = ' '*4
    tab6 = ' '*6
    tab8 = ' '*8
    
    extobjdef = ''
    intobjdef = ''
    extobj = set()

    vobjects  = '%s// cache pointers to filtered objects\n' % tab2
    vobjects += '%sobjects.clear();\n' % tab2

    for name, words, records in blocks['object']:
        if DEBUG > 0:
            print 'OBJECT( %s )' % name
            
        for record in records:
            t = split(record)
            token = t[0]
            if token == 'take':
                objname = t[1]
                if objname not in blocktypes['object']:
                    extobj.add(objname)
                    singleton = single.findall(lower(objname)) != []
                    if singleton:
                        extobjdef += '\nTEParticle %s;\n\n' % objname
                        SINGLETON_CACHE.add(name)
                        if DEBUG > 0:
                            print "\tsingleton object( %s )" % name
                    else:
                        extobjdef += 'vector<TEParticle> %s;\n' % objname
                            
        singleton = single.findall(lower(name)) != []
        if singleton:
            intobjdef += '\nTEParticle %s;\n\n' % name
        else:
            intobjdef += 'vector<TEParticle> %s;\n' % name

        vobjects += '%sobjects.push_back(&object_%s);\n' % (tab2, name)            
        
    objdef = '''// external objects
%s
// internal objects
%s
''' % (extobjdef, intobjdef)

    # -------------------------------------------------------
    runimpl     = '      %(analyzer)s.run(' % names
    runtab      = ' '*len(runimpl)
    
    runargs     = ''
    runargsimpl = 'void %(name)s_s::run(' % names
    
    bigtab      = ' '*len(runargsimpl)
    smalltab    = ' '*len('  void run')+' '

    adapter     = names['adapter']
    copyargsimpl= ''
    extobjimpl  = '\n%s// map external objects to internal ones\n' % tab6
    for name in extobj:
        singleton = single.findall(lower(name)) != []
        if singleton:
            rtype = 'TEParticle'
        else:
            rtype = 'std::vector<TEParticle>'        
        extobjimpl  += '%s%s %s;\n' % (tab6, rtype, name)

        rtype = rtype + '&'
        runargsimpl += '%s %s_,\n%s' % (rtype, name, bigtab)
        runargs     += '%s %s_,\n%s' % (rtype, name, smalltab)
        runimpl     += '%s,\n%s'  % (name, runtab)

        extobjimpl  += '%s%s(ev, "%s", \t%s);\n' % (tab6, adapter, name, name)
        copyargsimpl+= '  %s\t= %s_;\n' % (name, name)
        
    runimpl     = rstrip(runimpl)[:-1] + ');\n'
    runargs     = rstrip(runargs)[:-1]
    runargsimpl = rstrip(runargsimpl)[:-1] + ')\n'
 

    names['runimpl']     = runimpl
    names['runargs']     = runargs
    names['runargsimpl'] = runargsimpl
    names['copyargsimpl']= copyargsimpl
    
    # implement object selections
    objdef += '\n// object definitions\n'
    for name, words, records in blocks['object']:
        objdef += 'struct object_%s_s : public lhadaThing\n' % name
        objdef += '{\n'
        objdef += '%sobject_%s_s() : lhadaThing() {}\n' % (tab2, name)
        objdef += '%s~object_%s_s() {}\n' % (tab2, name) 
        objdef += '%svoid create()\n' % tab2
        objdef += '%s{\n' % tab2
        
        singleton = single.findall(lower(name)) != []
        if singleton:
            objdef += process_singleton_object(name, records, tab4, blocktypes)
        else:
            objdef += process_multiple_objects(name, records, tab4, blocktypes)

        objdef += '%s};\n' % tab2
        objdef += '} object_%s;\n\n' % name
        
    names['objdef']     = objdef   
    names['extobjimpl'] = extobjimpl
    names['vobjects']   = vobjects
#--------------------------------------------------------------------------------
def process_variables(names, blocks):
    if DEBUG > 0:
        print '\nBEGIN( process_variables )'

    if not blocks.has_key('variable'): return  ''
        
    tab2 = ' '*2
    vardef  = '// variables\n'
    varimpl = '%s// compute event level variables\n' % tab2
    for name, words, records in blocks['variable']:
        if DEBUG > 0:
            print 'VARIABLE( %s )' % name
            
        for record in records:
            t = split(record)
            token = t[0]
            
            if token == 'apply':
                # found an apply token
                func  = joinfields(t[1:], ' ')
                fname = split(func, '(')[0]
                # strip away preceding _
                #if fname[0] == '_': fname = fname[1:]

                if not blocks['function_info'].has_key(fname):
                    boohoo('''
    variable %s uses the function %s, 
    but the latter may not have been defined in the LHADA file.
    ''' % (name, fname))
                rtype, intname, extname, argtypes = blocks['function_info'][fname]
                func = replace(func, fname, intname)
                vardef  += '%s\t%s;\n' % (rtype, name)
                varimpl += '%s%s\t= %s;\n' % (tab2, name, func)
                
    names['vardef']  = vardef
    names['varimpl'] = varimpl
#--------------------------------------------------------------------------------
def process_cuts(names, blocks, blocktypes):
    if DEBUG > 0:
        print '\nBEGIN( process_cuts )'

    if not blocks.has_key('cut'):
        names['cutdef'] = ''
        
    cutdef  = '// selections\n'
    vcuts   = '  // cache pointers to cuts\n'
    vcuts  += '  cuts.clear();\n'
    #vcuts  += '  vector<lhadaThing*> cuts;\n'
    for name, words, records in blocks['cut']:    
        vcuts += '  cuts.push_back(&cut_%s);\n' % name
    
    # implement selections
    tab2 = ' '*2
    tab4 = tab2*2
      
    for name, words, records in blocks['cut']:
        if DEBUG > 0:
            print 'CUT( %s )' % name

        # get cut strings
        values = []
        for record in records:
            t = split(record)
            token = t[0]
            if token != 'select': continue
            value = joinfields(t[1:], ' ')
            values.append(value)

        cutdef += 'struct cut_%s_s : public lhadaThing\n' % name 
        cutdef += '{\n'
        cutdef += '  std::string name;\n'
        cutdef += '  double total;\n'
        cutdef += '  double dtotal;\n'
        cutdef += '  TH1F*  hcount;\n'
        cutdef += '  bool   done;\n'
        cutdef += '  bool   result;\n'
        cutdef += '  double weight;\n\n'
        cutdef += '  int    ncuts;\n\n'
        cutdef += '  cut_%s_s()\n' % name
        cutdef += '''    : lhadaThing(),
      name("%s"),
      total(0),
      dtotal(0),
      hcount(0),
      done(false),
      result(false),
      weight(1),
      ncuts(%d)
''' % (name, len(values))
           
        cutdef += '''  {
    hcount = new TH1F("cutflow_%s", "", 1, 0, 1);
    hcount->SetCanExtend(1);
    hcount->SetStats(0);
    hcount->Sumw2();

    hcount->Fill("none", 0);
''' % name
        
        for value in values:
            cutdef += '    hcount->Fill("%s", 0);\n' % nip.sub('', value)        
        cutdef += '  }\n\n'
        cutdef += '  ~cut_%s_s() {}\n\n' % name
        cutdef += '''  void summary(std::ostream& os)
  {
    os << name << std::endl;
    double gtotal = hcount->GetBinContent(1);
    for(int c=0; c <= ncuts; c++)
      {
        double value(hcount->GetBinContent(c+1));
        double error(hcount->GetBinError(c+1));
        double efficiency=0;
        if ( gtotal > 0 ) efficiency = value/gtotal;
        char record[1024];
        sprintf(record, 
                " %(percent)s2d %(percent)s-45s:"
                " %(percent)s9.2f +/- %(percent)s5.1f %(percent)s6.3f",
                c+1, hcount->GetXaxis()->GetBinLabel(c+1), 
                value, error, efficiency);
        os << record << std::endl;
      }
    os << std::endl;
  }
''' % {'percent': '%'}
        
        cutdef += '  void count(string c)\t\t{ hcount->Fill(c.c_str(), weight); }\n'
        cutdef += '  void write(TFile* fout)\t{ fout->cd(); hcount->Write(); }\n'
        cutdef += '  void reset()\t\t\t{ done = false; result = false; }\n'
        cutdef += '  bool operator()()\t\t{ return apply(); }\n\n'     
        cutdef += '  bool apply()\n'
        cutdef += '  {\n'
        cutdef +='''    if ( done ) return result;
    done   = true;
    result = false;
    count("none");

'''       
        for value in values:
            # convert to C++
            cutdef += '%sif ( !(%s) ) return false;\n' % \
              (tab4, convert2cpp(value, 'cut', blocktypes))
            cutdef += '%scount("%s");\n\n' % (tab4, nip.sub('', value))
        cutdef += '%stotal  += weight;\n'  % tab4
        cutdef += '%sdtotal += weight * weight;\n\n'  % tab4
        cutdef += '%s// NB: remember to update result cache\n' % tab4
        cutdef += '%sresult  = true;\n' % tab4
        cutdef += '%sreturn true;\n' % tab4
        cutdef += '  }\n'            
        cutdef += '} cut_%s;\n\n' % name

    names['cutdef'] = cutdef
    names['vcuts']  = vcuts
#--------------------------------------------------------------------------------
def main():

    # check if setup.sh has been sourced
    if not os.environ.has_key("LHADA2TNM_PATH"):
        boohoo('''
    please source setup.sh in lhada2tnm to define
    LHADA2TNM_PATH
        then try again!
''')
        
        
    filename, option = decodeCommandLine()
    names  = NAMES
    names['filename']    = filename
    names['name']        = option.name    
    names['treename']    = option.treename
    names['adaptername'] = option.adaptername

    # check that src and include directories exist
    if not os.path.exists('src'):
        boohoo('src directory not found')

    if not os.path.exists('include'):
        boohoo('include directory not found')

    if not os.path.exists('include/linkdef.h'):
        boohoo('include/linkdef not found')        

    if not os.path.exists('Makefile'):
        boohoo('Makefile not found')
    
    # copy TEParticle.h, TEParticle.cc, and requested adapter code to local area
    cmd = '''
cp $LHADA2TNM_PATH/external/include/TEParticle.h include/
cp $LHADA2TNM_PATH/external/include/%(adaptername)s.h include/ 
cp $LHADA2TNM_PATH/external/src/TEParticle.cc src/
cp $LHADA2TNM_PATH/external/src/%(adaptername)s.cc src/
''' % names
    os.system(cmd)    
    
    names['fundef']   = ''
    names['objdef']   = ''
    names['vardef']   = ''
    names['aodimpl']  = ''
    names['percent']  = '%'
    blocks = extractBlocks(filename)

    blocktypes = {}
    for btype in BLOCKTYPES:
        blocktypes[btype] = set()
        if not blocks.has_key(btype): continue
        for name, words, records in blocks[btype]:
            blocktypes[btype].add(name)

    if DEBUG > 0:
        printBlocks(blocks)

    process_info(names,      blocks)
    
    process_functions(names, blocks)

    process_objects(names,   blocks, blocktypes)

    process_variables(names, blocks)

    process_cuts(names,      blocks, blocktypes)

    # --------------------------------------------    
    # write out C++ code
    # --------------------------------------------

    record = TEMPLATE_CC % names
    open('src/%(name)s_s.cc' % names, 'w').write(record)

    record = TEMPLATE_HH % names
    open('include/%(name)s_s.h' % names, 'w').write(record)

    record = TNM_TEMPLATE_CC % names

    open('%(name)s.cc' % names, 'w').write(record)


    # update linkdef
    linkdef = strip(os.popen('find * -name "linkdef*"').read())
    if linkdef != '':
        names['linkdef'] = linkdef
        record = strip(os.popen('grep %(name)s_s %(linkdef)s' % names).read())
        if record == '':
            print 'update linkdef'
            record = strip(open(linkdef).read())
            records= split(record, '\n')[:-1]
            records.append('#pragma link C++ class lhadaThing;' % names)
            records.append('#pragma link C++ class %(name)s_s;' % names)            
            records.append('#pragma link C++ class TEParticle;' % names)
            records.append('#pragma link C++ class vector<TEParticle>;' % names)
            records.append('')
            records.append('#endif')
            record = joinfields(records, '\n')
            open(linkdef, 'w').write(record)


    # update Makefile
    makefile = strip(os.popen('find * -name "Makefile*"').read())
    if makefile != '':
        names['makefile'] = makefile
        record = strip(os.popen('grep %(name)s_s %(makefile)s' % names).read())
        if record == '':
            print 'update Makefile'
            names['makefile'] = makefile
            record = strip(open(makefile).read())
            tnm    = re.compile('tnm.h.*$', re.M)
            record = tnm.sub('tnm.h $(incdir)/%(name)s_s.h' % names, record)
            open(makefile, 'w').write(record)    
#--------------------------------------------------------------------------------     
try:
    main()
except KeyboardInterrupt:
    print
    print "ciao!"
    
