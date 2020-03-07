r"""
## PypTeX: the Python Preprocessor for TeX

### Author: Sébastien Loisel

PypTeX is the Python Preprocessor for LaTeX. It allows one to embed Python
code fragments in a LaTeX template file.

# Requirements

This has been tested on Mac with TeXLive 2017. PypTeX itself can be installed
via `pip install pyptex`.

# Introduction

Assume `example.tex` contains the following text:

    \documentclass{article}
    @{from sympy import *}
    \begin{document}
    $$\int x^3\,dx = @{S('integrate(x^3,x)')}+C$$
    \end{document}

The command `pyptex example.tex` will generate `example.pdf`,
as well as the intermediary file `example.pyptex`. PypTeX works by extracting Python 
fragments in `example.tex` indicated by either `@{...}` or `@{{{...}}}` and substituting the
corresponding outputs to produce `example.pyptex`, which is then compiled with 
`pdflatex example.pyptex`, although one can use any desired LaTeX processor in lieu of
`pdflatex`. The intermediary file `example.pyptex` is pure LaTeX.

When processing Python fragments, the global scope contains an object `pyp` that is a
(weakref proxy for a) `pyptex.pyptex` object that makes available several helper functions
and useful data. For example, `pyp.print("hello, world")` inserts the string `hello, world` 
into the generated `example.pyptex` file.

# A slightly bigger example.

[This](https://github.com/sloisel/pyptex/blob/master/examples/matrixinverse.tex) PypTeX source file produces
[this](https://github.com/sloisel/pyptex/blob/master/examples/matrixinverse.pdf) example of a matrix inversion by 
augmented matrix approach.

# Template preprocessing vs embedding

PypTeX is a template preprocessor for LaTeX based on the Python language. When Python
is embedded into LaTeX, Python code fragments are identified by LaTeX commands that use
standard TeX notation, such as `\py{...}`. The code extraction is performed by TeX, then
the code fragments are executed by Python, finally TeX is run again to merge the
Python-generated LaTeX fragments back into the master file.

By contrast, PypTeX is a preprocessor that extracts Python code fragments indicated by
`@{...}` using regular expressions. Once the relevant Python outputs are collected, they
are also inserted by regular expressions. LaTeX is only invoked once, on the final output.

There may be specialized cases where Python embeddings are preferred, but we found
that template preprocessing is superior to embedding. There are many reasons (that
will be described elsewhere in detail) but we briefly mention the following reasons:
1. Embeddings can result in deadlock. If we have `\includegraphics{dog.png}`, but 
`dog.png` is generated by a Python fragment, the first run of LaTeX will fail because
`dog.png` does not yet exist. Since LaTeX failed, it did not extract the Python fragments
and we cannot run the Python code that would generate `dog.png` unless we temporarily
delete the `\includegraphics{dog.png}` from `a.tex`. In our experience, deadlock
occurs almost every time we edit our large `.tex` files.
2. Embedding makes debugging difficult. By contrast, PypTeX treats Python's debugger Pdb 
as a first-class citizen and everything should work as normal. Please let us know if some
debugging task somehow fails for you.
3. Performance. Substituting using regular expressions is faster than running the
LaTeX processor.

# Pretty-printing template strings from Python with `pp(...)`

The function `pp(X)` pretty-prints the template string `X` with substitutions
from the local scope of the caller. This is useful for medium length LaTeX fragments
containing a few Python substitutions:
```python
>>> from pyptex import pp
>>> from sympy import *
>>> p = S('x^2-2*x+3')
>>> dpdx = p.diff(S('x'))
>>> x0 = solve(dpdx)[0]
>>> pp('The minimum of $y=@p$ is at $x=@x0$.')
'The minimum of $y=x^{2} - 2 x + 3$ is at $x=1$.'
```

# Caching

When compiling `a.tex`, PypTeX creates a cache file `a.pickle`. This file is
automatically invalidated if the Python fragments in `a.tex` change, or if some
other dependencies have changed. Dependencies can be declared from inside `a.tex` via
`pyp.dep(...)`. Caching can be completely disabled with `pyp.disable_cache=True`,
and users can delete `a.pickle` as necessary.

# Scopes

For each template file `a.tex`, `b.tex`, ... a private global scope is created for
executing Python fragments. This means that Python fragments in `a.tex` cannot use
functions or variables defined in `b.tex`, although shared functions could be
implemented in a shared `c.py` Python module that is `import`ed into
`a.tex` and `b.tex`.

In particular, when does `pyp.input('b.tex')` from `a.tex`, the code in `b.tex` cannot
use functions and data generated in `a.tex`. This means that `b.tex` is effectively
a "compilation unit" whose semantics are essentially independent of `a.tex`.

For any given `a.tex` file, its private global scope is initialized with the 
standard Python builtins and with a single `pyp` object, which is a `weakref.proxy` 
to the `pyptex('a.tex')` instance. We use a `weakref.proxy` because the global
scope of `a.tex` is a `dict` stored in the (private) variable `pyp.__global__`. The
use of `weakref.proxy` avoids creating a circular data structure that would otherwise
stymie the Python garbage collector. For most purposes, this global `pyp` variable
acts exactly like a concrete `pyptex` instance.
"""

import sys
import sympy
import re
import os
import traceback
import numpy
import glob
import pickle
import time
import subprocess
import datetime
import weakref
import string
import inspect

__pdoc__ = {"pyptex.compile":False,
            "pyptex.generateddir":False,
            "pyptex.process":False,
            "pyptex.resolvedeps":False,
            "pyptex.run":False,}

pypparser = re.compile(r'((?<!\\)%[^\n]*\n)|(@@{)|(@{([^{}]+)}|@{{{(.*?)}}})',re.DOTALL)

__pdoc__["format_my_nanos"] = False
# Credit: abarnet on StackOverflow
def format_my_nanos(nanos):
    """Convert nanoseconds to a human-readable format"""
    dt = datetime.datetime.fromtimestamp(nanos / 1e9)
    return '{}.{:09.0f}'.format(dt.strftime('%Y-%m-%d@%H:%M:%S'), nanos % 1e9)

__pdoc__["dictdiff"] = False
def dictdiff(A,B):
    A = set(A.items())
    B = set(B.items())
    D = A ^ B
    if(len(D)==0):
        return None
    return next(iter(D))

__pdoc__["mylatex"] = False
def mylatex(X):
    return sympy.latex(X) if X!=None else ""

__pdoc__["latextemplate"] = False
class latextemplate(string.Template):
    delimiter = "@"

__pdoc__["LatexDict"] = False
class LatexDict:
    def __init__(self, glob, loc):
        self.loc = loc
        self.glob = glob

    def __getitem__(self, key):
        return mylatex(self.loc[key] if key in self.loc else self.glob[key])

def pp(Z,levels=1):
    r"""
    Pretty-prints the template text string `Z`, using substitutions from the local
    scope that is `levels` calls up on the stack. The template character is @.

    For example, assume the caller has the value `x=3` in its local variables. Then,
    `pp("$x=@x$")` produces `$x=3$`.
    """
    foo = inspect.currentframe()
    while(levels>0):
        foo = foo.f_back
        levels = levels-1
#    foo = LatexDict({k: v for d in [foo.f_globals, foo.f_locals] for k, v in d.items()})
    foo = LatexDict(foo.f_globals, foo.f_locals)
    D = latextemplate(Z)
    txt = D.substitute(foo)
    return txt


class pyptex:
    r"""Class `pyptex.pyptex` is used to parse an input (templated) `a.tex` file
    and produce an output `a.pyptex` file, and can be used as follows:
        `pyp = pyptex('a.tex')`
    The constructor reads `a.tex`, executes Python fragments and performs relevant
    substitutions, writing `a.pyptex` to disk. The contents of `a.pyptex` are also
    available as `pyp.compiled`.
    """
    def genname(self,pattern="fig{gencount}.eps"):
        r"""Generate a filename
        
        To produce an automatically generated filename, use the statement
        `pyp.genname()`, where `pyp` is an object of type `pyptex`, for parsing a
        given file `a.tex`. By default, this will generate the name 
        `'a-generated/fig{gencount}.eps'`
        The subdirectory can be overridden by overwriting `pyp.gendir`,
        and `gencount` denotes `pyp.gencount`. Any desired pattern can be used,
        for example:
            `name = pyp.genname('hello-{gencount}-{thing}.txt')`
        will return something like `'a-generated/hello-X-Y.txt'`, where
        `X` is `pyp.gencount` and `Y` is `pyp.thing`.

        `pyp.genname()` does not actually create the file. `pyp.genname()` increments
        `pyp.gencount` every time it is called.
        """
        self.gencount = self.gencount+1
        return self.gendir+'/'+pattern.format(**self.__dict__)

    def savefig(self,fig,pattern="fig{gencount}.eps",**kwargs):
        """Save a figure to the a-generated/* subdirectory.
        
        If `pyp` is an object of type `pyptex`:
        `pyp.savefig(fig)` saves a SymPy or matplotlib figure to the `a-generated/*` 
        subdirectory, using the `pyptex.genname()` automatically generated filename. 
        `pyp.savefig(fig,pattern)` further specifies the filename pattern of the
        generated name, see `genname()`.
        `pyp.savefig(fig,pattern,...)` passes any further keyword arguments directly
        to the `savefig` function from matplotlib.

        A typical way of using this from a TeX file is:
        `\\includegraphics{@{pyp.savefig(...)}}`
        """
        if(self.__sympy_plot__ == None):
            self.__sympy_plot__ = sympy.plotting.plot(1,show=False).__class__
        figname = self.genname(pattern)
        if(fig.__class__==self.__sympy_plot__):
            backend = fig.backend(fig)
            backend.process_series()
            backend.fig.savefig(figname, **kwargs)
        else:
            fig.savefig(figname, **kwargs)
        self.dep(figname)
        return figname
    def generateddir(self):
        """This is an internal function that creates the generated directory"""
        self.gendir = self.filename+'-generated'
        if not os.path.exists(self.gendir):
            os.makedirs(self.gendir)
        self.gencount = 0

    def __init__(self,texfilename,argv=[],latexcommand=False):
        r"""`pyp = pyptex('a.tex')` reads in the LaTeX file a.tex and locates all
        Python code fragments contained inside. These Python code fragments are
        executed and their outputs are substituted to produce the `a.pyptex` output file.

        `pyp = pyptex('a.tex',argv)` passes "command-line arguments". The pyptex
        command-line passes `sys.argv[2:]` for this parameter. If omitted, `argv`
        defaults to `[]`. If using PypTeX as an templating engine to generate
        multiple documents from a single source `a.tex` file, one should use
        the `argv` parameter to pass in the various side-parameters needed to generate
        each document. For example, `a.tex` might have the line "Dear @{pyp.argv[0]}""
        One could produce a letter to John by doing `pyp = pyptex('a.tex',['John'])`.

        `pyp = pyptex('a.tex',argv,latexcommand)` further executes a specific shell
        command once `a.pyptex` has been written to disk (e.g. `pdflatex {pytexfilename}`).
        The default value of `latexcommand` is `False`, in which case no shell command
        is executed.

        Some salient fields of the `pyp=pyptex('a.tex')` class are:

        * `pyp.filename = 'a'` (so `a.tex`, with the extension stripped)
        * `pyp.texfilename = 'a.tex'`
        * `pyp.cachefilename = 'a.pickle'`
        * `pyp.bibfilename = 'a.bib'`, used by the `pyp.bib()` function
        * `pyp.pyptexfilename = 'a.pyptex'`
        * `pyp.auxfilename = 'a.aux'`, useful in case bibtex is used
        * `pyp.latex = "pdflatex --file-line-error --synctex=1"`
          One may overwrite this in a.tex to choose a different latex engine, e.g.
          `pyp.latex = "latex"`
        * `pyp.latexcommand` defaults to `False`, but the command-line version of `pyptex
          uses something like
          `r"{latex} {pyptexfilename} && (test ! -f {bibfilename} || bibtex {auxfilename})"`
          The relevant substitutions are performed by `string.format` from `pyp.__dict__`
        * `pyp.disable_cache = False`, set this to `True` if you want to disable the `a.pickle`
          cache. You shouldn't need to do this but if your Python code is nondeterministic
          or if tracking dependencies is too hard, disabling all caching will ensure
          that `a.pyptex` is correctly compiled into `a.pdf` and that a stale cache is
          never used.
        * `pyp.deps` is a dictionary of dependencies and timestamps
        * `pyp.lc` counts lines while parsing
        * `pyp.argv` stores the ``command-line arguments'' for template generation
        * `pyp.exitcode` is the exit code of the `pyp.latexcommand`
        * `pyp.gencount` is the counter for generated files (see `pyp.gen()`)
        * `pyp.fragments` is the list of Python fragments extracted from a.tex
        * `pyp.outputs` is the matching outputs.
        * `pyp.compiled` is the string that is written to `a.pyptex`
        """
        print(texfilename+": pyptex compilation begins")
        self.__globals__ = {"__builtins__":__builtins__,"pyp":weakref.proxy(self)}
        self.filename = os.path.splitext(texfilename)[0]
        self.texfilename = texfilename
        self.cachefilename = self.filename+'.pickle'
        self.bibfilename = self.filename+'.bib'
        self.pyptexfilename = self.filename+'.pyptex'
        self.auxfilename = self.filename+'.aux'
        self.latex = "pdflatex --file-line-error --synctex=1"
        self.latexcommand = latexcommand
        self.disable_cache = False
        self.deps = {}
        self.lc = 0
        self.argv = argv
        self.__sympy_plot__ = None
        self.exitcode = 0
        self.generateddir()
        self.dep(__file__)
        self.compile()
        print(texfilename+": pyptex compilation ends")


    def run(self,S,k):
        """An internal function for executing Python code"""
        print("Executing Python code:\n"+S)
        S = "\n"*k+S
        glob = self.__globals__
        doeval = False
        self.accum = []
        try:
            C = compile(S,self.texfilename,mode='eval')
            doeval = True
        except:
            pass
        if(doeval):
            ret = eval(C,glob)
            self.accum.append(ret)
        else:
            C = compile(S,self.texfilename,mode='exec')
            exec(C,glob)
        print("Python result:\n"+str(self.accum))
        return self.accum
    def print(self,*argv):
        """If `pyp` is an object of type `pyptex`, `pyp.print(X)` causes `X` to be converted
        to its latex representation and substituted into the `a.pyptex` output file.
        The conversion is given by `sympy.latex(X)`, except that `None` is converted
        to the empty string.

        Many values can be printed at once with the notation `pyp.print(X,Y,...)`"""
        self.accum.extend(argv)

    def process(self,S,runner):
        """An internal helper function for parsing the input file"""
        SS = S.splitlines()
        ln = numpy.cumsum(numpy.array(numpy.array(list(S),dtype='U1')=='\n',int))
        ln = numpy.insert(ln,0,0)
        def dowork(m):
            if(m.start(1)>=0):
                return m.group(0)
            if(m.start(2)>=0):
                return '@{'
            for k in range(4,6):
                if(m.start(k)>=0):
                    z = m.group(k)
                    z0 = m.start(k)
                    z1 = m.end(k)
            self.lc = self.lc + (ln[z1]-ln[z0])+1
            return runner(z,ln[z0])
        return pypparser.sub(dowork,S)

    def compile(self):
        """An internal function for compiling the input file"""
        with open(self.texfilename,'rt') as file:
            text = file.read()
        try:
            with open(self.cachefilename,"rb") as file:
                cache = pickle.load(file)
        except:
            cache = {}
        defaults = {"fragments":[],
                    "outputs":[],
                    "deps":{},
                    "argv":[],
                    "disable_cache":True,
                    }
        for k,v in defaults.items():
            if(not k in cache):
                cache[k] = v
        self.fragments = []
        def scanner(C,k):
            self.fragments.append(C)
            return ""
        self.process(text,runner = scanner)
        print("Found "+str(self.lc)+" lines of Python.")
        saveddeps = self.deps
        self.deps = {}
        for k in cache["deps"]:
            self.dep(k)
        self.resolvedeps()
        cached = True
        if(cache["disable_cache"]):
            print("disable_cache=True")
            cached = False
        elif(cache["argv"]!=self.argv):
            print("argv differs",self.argv,cache["argv"])
            cached = False
        elif(cache["fragments"]!=self.fragments):
            F1 = dict(enumerate(cache["fragments"]))
            F2 = dict(enumerate(self.fragments))
            k = dictdiff(F1,F2)[0]
            print("Fragment #",k,
                  "\nCached version:\n",F1[k] if k in F1 else None,
                  "\nLive version:\n",F2[k] if k in F2 else None)
            cached = False
        elif(self.deps!=cache["deps"]):
            F1 = cache["deps"]
            F2 = self.deps
            k = dictdiff(F1,F2)[0]
            print("Dependency mismatch",k,
                  "\nCached version:\n",F1[k] if k in F1 else None,
                  "\nLive version:\n",F2[k] if k in F2 else None)
            cached = False
        if(cached):
            print("Using cached Python outputs")
            for k,v in cache.items():
                self.__dict__[k] = v 
            self.subcount = -1
            def subber(C,k):
                self.subcount = self.subcount+1
                return self.outputs[self.subcount]

            self.compiled = self.process(text,runner = subber)
        else:
            print("Cache is invalidated.")
            self.deps = saveddeps
            self.outputs = []
            def appender(C,k):
                result = self.run(C,k)
                self.outputs.append("".join(map(mylatex,result)))
                return self.outputs[-1]
            self.compiled = self.process(text,runner = appender)
            writecache = True
        sys.stdout.flush()
        if(self.pyptexfilename):
            print("Saving to file: "+self.pyptexfilename)
            with open(self.pyptexfilename,'wt') as file:
                file.write(self.compiled)
        self.resolvedeps()
        print("Dependencies are:\n"+str(self.deps))
        if(cached==False):
            print("Saving cache file",self.cachefilename)
            with open(self.cachefilename,'wb') as file:
                cache = {}
                for k,v in self.__dict__.items():
                    if(k[0:2]=='__' and k[-2:]=='__'):
                        pass
                    elif(callable(v)):
                        pass
                    else:
                        cache[k]=v
                print("Caching:",cache.keys())
                pickle.dump(cache,file)
        if(self.latexcommand):
            cmd = self.latexcommand.format(**self.__dict__)
            print("Running Latex command:\n"+cmd)
            self.exitcode = os.system(cmd)
    def bib(self,bib):
        """A helper function for creating a `.bib` file. If `pyp=pyptex('a.tex')`,
        then `pyp.bib('''@book{knuth1984texbook, title={The {TEXbook}}, 
        author={Knuth, Donald Ervin and Bibby, Duane}}''')` creates a file
        `a.bib` with the given text. This is just a convenience function
        that makes it easier to incorporate the bibtex file straight into the
        `a.tex` source. In `a.tex`, the typical way of using it is:
        `\\bibliography{@{{{pyp.bib("...")}}}}`
        """
        with self.open(self.bibfilename,'wt') as file:
            file.write(bib)
        return self.filename
    def dep(self,filename):
        """
        If `pyp=pyptex('a.tex')`, then `pyp.dep(filename)` declares that the Python code
        in `a.tex` depends on the file designated by `filename`. When the object
        `pyptex('a.tex')` is constructed, the file `a.pickle` will be loaded (if it exists).
        `a.pickle` is a cache of the results of the Python calculactions in `a.tex`. 
        If the cache is deemed valid, the `pyptex` constructor does not rerun all 
        the Python fragments in `a.tex` but instead uses the previously cached outputs.

        The cache is invalidated under the following scenarios:
        1. The new Python fragments in `a.tex` are not identical to the cached fragments.
        2. The "last modification" timestamp on dependencies is not the same as in the cache.
        3. `pyp.disable_cache==True`

        The list of dependencies defaults to only the `pyptex` executable. Additional 
        dependencies can be manually declared via `pyp.dep(filename)`.

        For convenience, `pyp.dep(filename)` returns filename.
        """
        self.deps[filename] = ""
        return filename
    def resolvedeps(self):
        """An internal function that actually computes the datestamps of dependencies"""
        for k in self.deps:
            try:
                ds = format_my_nanos ( os.stat(k).st_mtime_ns )
            except:
                ds = ""
            self.deps[k] = ds

    def input(self,filename,argv=False):
        r"""If `pyp = pyptex('a.tex')` then 
        `pyp.input('b.tex')`
        return the string `\input{"b.pyptex"}`. The common way of using this is to
        put `@{pyp.input('b.tex')}` somewhere in `a.tex`.
        The function `pyp.input('b.tex')` internally calls the constructor
        `pyptex('b.tex')` so that `b.pyptex` is compiled from `b.tex`.

        Note that the two files `a.tex` and `b.tex` are "semantically isolated". All
        calculations, variables and functions defined in `a.tex` live in a global scope
        that is private to `a.tex`, much like each Python module has a private global
        scope. In a similar fashion, `b.tex` has its own private global scope.
        The global `pyp` objects in `a.tex` and `b.tex` are also different instances
        of the `pyptex` class. This is similar to the notion of "compilation units" in 
        the C programming language.

        If one wishes to pass some parameters from `a.tex` to `b.tex`, one may use
        the notation `pyp.input('b.tex',argv)`, which will initialize the global
        `pyp` object of `b.tex` so that it contains the field `pyp.argv=argv`.

        If one absolutely needs to export variables from `b.tex` back to `a.tex`, one
        should directly use the `pyptex` constructor, e.g. `pyp_b = pyptex('b.tex',argv)`;
        one can then retrieve values from the `b.tex` scope, e.g. with `pyp_b.fragments[0]`.
        """
        ret=pyptex(filename,argv or self.argv,False)
        return r"\input{"+ret.pyptexfilename+"}"
    def open(self,filename,*argv,**kwargs):
        """If pyp = pyptex('a.tex') then pyp.open(filename,...) is a wrapper for
        the builtin function open(filename,...) that further adds filename to
        the list of dependencies via pyp.dep(filename).
        """
        self.dep(filename)
        return open(filename,*argv,**kwargs)

def pyptexmain(argv=None):
    """
    This function parses an input file a.tex to produce a.pyptex and a.pdf, by
    doing pyp = pyptex('a.tex',...) object. The filename a.tex must be in argv[1];
    if argv is not provided, it is taken from sys.argv.
    The default pyp.latexcommand invokes pdflatex and, if a.bib is present, also bibtex.
    If an exception occurs, pdb is automatically invoked in postmortem mode.
    If "--pdb=no" is in argv, it is removed from argv and automatic pdb postmortem is disabled.
    If "--pdb=yes" is in argv, automatic pdb postmortem is enabled. This is the default.
    """
    argv = argv or sys.argv
    dopdb = True
    try:
        k = argv.index('--pdb=no')
        argv.pop(k)
        dopdb = False
    except:
        pass
    try:
        k = argv.index('--pdb=yes')
        argv.pop(k)
        dopdb = True
    except:
        pass
    if len(argv)<2:
        print("Usage: pyptex <filename.tex> ...")
        sys.exit(1)
    try:
        # logging inspired by Jacob Gabrielson on Stackoverflow
        tee = subprocess.Popen(["tee", os.path.splitext(argv[1])[0]+".pyplog"], stdin=subprocess.PIPE)
        os.dup2(tee.stdin.fileno(), sys.stdout.fileno())
        os.dup2(tee.stdin.fileno(), sys.stderr.fileno())
        pyp = pyptex(argv[1],argv[2:],
            latexcommand=r"{latex} {pyptexfilename} && (test ! -f {bibfilename} || bibtex {auxfilename})"
            )
    except:
        import pdb
        traceback.print_exc(file=sys.stdout)
        if(dopdb):
            print("A Python error has occurred. Launching the debugger pdb.\nType 'help' for a list of commands, and 'quit' when done.")
            pdb.post_mortem()
            sys.exit(1)
    return pyp.exitcode

