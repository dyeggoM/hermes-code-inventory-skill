using System.Text.Json;
using Microsoft.Build.Locator;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.MSBuild;
string? Get(string k){var i=Array.IndexOf(args,k);return i>=0&&i+1<args.Length?args[i+1]:null;}
if(args.Length==0||args[0]!="analyze-file"){Console.Error.WriteLine("analyze-file --root <dir> --file <path>");return 2;}
var root=Path.GetFullPath(Get("--root")??".");var file=Path.GetFullPath(Get("--file")??"");
if(!File.Exists(file))return 2;
if(!MSBuildLocator.IsRegistered)MSBuildLocator.RegisterDefaults();
using var ws=MSBuildWorkspace.Create();
var diagnostics=new List<object>();var decls=new List<object>();var calls=new List<object>();
Solution? sol=null;Project? proj=null;
static IEnumerable<string> FindProjects(string root){
 var skip=new HashSet<string>(StringComparer.OrdinalIgnoreCase){".git","bin","obj",".code-inventory","node_modules",".vs","packages"};
 var slns=new List<string>();var csprojs=new List<string>();
 var stack=new Stack<string>();stack.Push(root);
 while(stack.Count>0){var dir=stack.Pop();
  foreach(var f in Directory.EnumerateFiles(dir,"*.sln"))slns.Add(f);
  foreach(var f in Directory.EnumerateFiles(dir,"*.csproj"))csprojs.Add(f);
  foreach(var d in Directory.EnumerateDirectories(dir))if(!skip.Contains(Path.GetFileName(d)))stack.Push(d);}
 return slns.Concat(csprojs);
}
try{
 var all=FindProjects(root).ToList();
 var sln=all.FirstOrDefault(p=>p.EndsWith(".sln",StringComparison.OrdinalIgnoreCase));
 var nearest=all.Where(p=>p.EndsWith(".csproj",StringComparison.OrdinalIgnoreCase))
   .Where(p=>file.StartsWith(Path.GetDirectoryName(p)!+Path.DirectorySeparatorChar,StringComparison.Ordinal))
   .OrderByDescending(p=>(Path.GetDirectoryName(p)??"").Length).FirstOrDefault();
 if(sln!=null)sol=await ws.OpenSolutionAsync(sln);else if(nearest!=null)proj=await ws.OpenProjectAsync(nearest);
 if(sol!=null)proj=sol.Projects.FirstOrDefault(p=>p.Documents.Any(d=>Path.GetFullPath(d.FilePath??"")==file));
 if(sln==null&&nearest==null)diagnostics.Add(new{severity="warning",message="No .sln or .csproj found under "+root+"."});
}catch(Exception e){diagnostics.Add(new{severity="error",message=e.Message});}
if(proj==null)diagnostics.Add(new{severity="warning",message="No containing project loaded; semantic binding is unavailable."});
var doc=proj?.Documents.FirstOrDefault(d=>Path.GetFullPath(d.FilePath??"")==file);
var tree=doc!=null?await doc.GetSyntaxTreeAsync():null;
tree??=CSharpSyntaxTree.ParseText(await File.ReadAllTextAsync(file),path:file);
var model=doc!=null?await doc.GetSemanticModelAsync():null;
if(model!=null&&model.Compilation.References.Count()==0)
 diagnostics.Add(new{severity="warning",message="Compilation loaded with zero metadata references: the SDK used to build the project ("+Environment.Version+") cannot resolve its target framework. Calls stay unresolved until a matching SDK is installed."});
var rootNode=await tree.GetRootAsync();
string Key(ISymbol? s){var d=s?.OriginalDefinition;return d==null?"":d.GetDocumentationCommentId()??d.ToDisplayString(SymbolDisplayFormat.FullyQualifiedFormat);}
foreach(var n in rootNode.DescendantNodes().Where(n=>n is BaseTypeDeclarationSyntax or MethodDeclarationSyntax or ConstructorDeclarationSyntax or PropertyDeclarationSyntax or FieldDeclarationSyntax or NamespaceDeclarationSyntax or FileScopedNamespaceDeclarationSyntax or LocalFunctionStatementSyntax)){
 var sym=model?.GetDeclaredSymbol(n);if(sym==null)continue;
 var kind=n switch{NamespaceDeclarationSyntax or FileScopedNamespaceDeclarationSyntax=>"namespace",BaseTypeDeclarationSyntax=>"type",ConstructorDeclarationSyntax=>"constructor",MethodDeclarationSyntax=>"method",LocalFunctionStatementSyntax=>"local_function",PropertyDeclarationSyntax=>"property",FieldDeclarationSyntax=>"field",_=>"member"};
 var par=n.Parent?.AncestorsAndSelf().FirstOrDefault(x=>x is BaseTypeDeclarationSyntax or NamespaceDeclarationSyntax or FileScopedNamespaceDeclarationSyntax);
 decls.Add(new{kind,name=sym.Name,symbol_key=Key(sym),parent_symbol_key=par==null?null:Key(model?.GetDeclaredSymbol(par)),signature=sym.ToDisplayString(SymbolDisplayFormat.MinimallyQualifiedFormat),line=tree.GetLineSpan(n.Span).StartLinePosition.Line+1});
}
foreach(var inv in rootNode.DescendantNodes().OfType<InvocationExpressionSyntax>()){
 var target=model?.GetSymbolInfo(inv).Symbol;
 var callerNode=inv.Ancestors().FirstOrDefault(x=>x is BaseMethodDeclarationSyntax or LocalFunctionStatementSyntax or AccessorDeclarationSyntax);
 var caller=callerNode==null?null:model?.GetDeclaredSymbol(callerNode);if(caller==null)continue;
 calls.Add(new{caller_symbol_key=Key(caller),callee_symbol_key=target==null?null:Key(target),callee_name=target?.ToDisplayString(SymbolDisplayFormat.MinimallyQualifiedFormat)??inv.Expression.ToString(),external=target==null||!SymbolEqualityComparer.Default.Equals(target.ContainingAssembly,model!.Compilation.Assembly),line=tree.GetLineSpan(inv.Span).StartLinePosition.Line+1});
}
Console.WriteLine(JsonSerializer.Serialize(new{declarations=decls,calls,diagnostics}));return 0;
