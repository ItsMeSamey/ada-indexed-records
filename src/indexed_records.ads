with Ada.Containers.Vectors;
with Ada.Streams;
with Ada.Streams.Stream_IO;
with Interfaces;

package Indexed_Records is
   subtype U64 is Interfaces.Unsigned_64;
   subtype Bytes is Ada.Streams.Stream_Element_Array;
   type Group_Mode is (Grouped, Per_Record, Monolithic);
   Format_Error : exception;

   type Writer is limited private;
   procedure Create
     (W : in out Writer; Path : String; Block_Target : Positive := 262_144;
      Max_Record_Bytes : Positive := 2_097_152; Mode : Group_Mode := Grouped);
   procedure Append (W : in out Writer; Data : Bytes);
   procedure Close (W : in out Writer);
   procedure Abort_Write (W : in out Writer);

   type Reader is limited private;
   procedure Open
     (R : in out Reader; Path : String; Max_Index_Bytes : Positive := 67_108_864;
      Max_Record_Bytes : Positive := 2_097_152);
   procedure Close (R : in out Reader);
   function Count (R : Reader) return U64;
   function Read (R : in out Reader; Ordinal : U64) return Bytes;
   procedure Validate (R : in out Reader);
   procedure Unpack (R : in out Reader; Output_Path : String);
   function Frame_Count (R : Reader) return Natural;
   function Compressed_Size (R : Reader; Frame : Positive) return U64;
   function Decoded_Size (R : Reader; Frame : Positive) return U64;
   function Archive_Bytes (R : Reader) return U64;
   function Index_Bytes (R : Reader) return U64;
   function Bytes_Read (R : Reader) return U64;
   function Bytes_Decoded (R : Reader) return U64;

private
   use type U64;
   type Byte_Access is access Bytes;
   type Frame_Info is record
      Offset, Compressed, Decoded, Records, First : U64 := 0;
   end record;
   package Frames is new Ada.Containers.Vectors (Positive, Frame_Info);
   type Writer is limited record
      File : Ada.Streams.Stream_IO.File_Type;
      Buffer : Byte_Access;
      Used : Natural := 0;
      Block_Target : Positive := 262_144;
      Max_Record : Positive := 2_097_152;
      Mode : Group_Mode := Grouped;
      Group_Records, Total : U64 := 0;
      Table : Frames.Vector;
      Active : Boolean := False;
   end record;
   type Reader is limited record
      File : Ada.Streams.Stream_IO.File_Type;
      Table : Frames.Vector;
      Total, File_Bytes, Metadata_Bytes : U64 := 0;
      Read_Bytes, Decoded_Bytes : U64 := 0;
      Max_Record : Positive := 2_097_152;
      Active : Boolean := False;
   end record;
end Indexed_Records;
