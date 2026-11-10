// Cyclomatic complexity of every function declaration in the Go files named on stdin (a JSON array
// of paths), printed as a JSON array of {file, name, start, end, ccn}. Run with `go run qg_goccn.go`
// from a directory without go.mod. Counting rules are in qg_complexity.py's docstring: 1 + if, for
// (incl. range), case clauses except default, select cases except default, && and ||. Function
// literals count toward the declaration that contains them.
package main

import (
	"encoding/json"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
)

type function struct {
	File  string `json:"file"`
	Name  string `json:"name"`
	Start int    `json:"start"`
	End   int    `json:"end"`
	Ccn   int    `json:"ccn"`
}

func receiverName(expr ast.Expr) string {
	switch t := expr.(type) {
	case *ast.StarExpr:
		return receiverName(t.X)
	case *ast.IndexExpr:
		return receiverName(t.X)
	case *ast.IndexListExpr:
		return receiverName(t.X)
	case *ast.Ident:
		return t.Name
	}
	return "?"
}

func complexity(body ast.Node) int {
	count := 1
	ast.Inspect(body, func(n ast.Node) bool {
		switch t := n.(type) {
		case *ast.IfStmt, *ast.ForStmt, *ast.RangeStmt:
			count++
		case *ast.CaseClause:
			if t.List != nil {
				count++
			}
		case *ast.CommClause:
			if t.Comm != nil {
				count++
			}
		case *ast.BinaryExpr:
			if t.Op == token.LAND || t.Op == token.LOR {
				count++
			}
		}
		return true
	})
	return count
}

func main() {
	var paths []string
	if err := json.NewDecoder(os.Stdin).Decode(&paths); err != nil {
		panic(err)
	}
	out := []function{}
	for _, path := range paths {
		fset := token.NewFileSet()
		file, err := parser.ParseFile(fset, path, nil, parser.SkipObjectResolution)
		if err != nil {
			os.Stderr.WriteString(err.Error() + "\n")
			os.Exit(2)
		}
		for _, decl := range file.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			if !ok || fn.Body == nil {
				continue
			}
			name := fn.Name.Name
			if fn.Recv != nil && len(fn.Recv.List) > 0 {
				name = receiverName(fn.Recv.List[0].Type) + "." + name
			}
			out = append(out, function{path, name, fset.Position(fn.Pos()).Line, fset.Position(fn.End()).Line, complexity(fn.Body)})
		}
	}
	json.NewEncoder(os.Stdout).Encode(out)
}
